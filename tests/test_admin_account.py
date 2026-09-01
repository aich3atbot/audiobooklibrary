"""The startup reconciliation of the admin account against ADMIN_PASSWORD.

Each test runs against its own empty database, so the shared test database
(which conftest seeds with an admin) is never disturbed.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.conftest import cheap_password_hash


@pytest.fixture
def fresh_db(test_settings, tmp_path):
    """An empty database with the full schema and no admin row."""
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def admin_password(test_settings, monkeypatch):
    """Set ADMIN_PASSWORD for the duration of a test."""

    def setter(value: str):
        monkeypatch.setattr(test_settings, "admin_password", value)

    return setter


def stored_admin(session):
    from app.models import User

    return session.scalar(select(User).where(User.username == "admin"))


def test_creates_the_account_on_a_fresh_database(fresh_db, admin_password):
    from app.models import UserRole
    from app.passwords import verify_password
    from app.services.users import ensure_admin_account

    admin_password("first-run")
    ensure_admin_account(fresh_db)

    admin = stored_admin(fresh_db)
    assert admin is not None
    assert admin.role == UserRole.ADMIN
    assert admin.is_admin and not admin.is_limited
    assert admin.enabled
    assert admin.hardcover_token == ""
    assert verify_password("first-run", admin.password_hash)


def test_fresh_database_without_a_password_refuses_to_start(fresh_db, admin_password):
    from app.services.users import ensure_admin_account

    admin_password("")
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        ensure_admin_account(fresh_db)
    assert stored_admin(fresh_db) is None


def test_existing_account_without_a_password_is_left_alone(fresh_db, admin_password):
    """Once the account exists the database is the record: unsetting the
    variable does not lock anyone out, and does not change the password."""
    from app.services.users import ensure_admin_account

    admin_password("original")
    ensure_admin_account(fresh_db)
    hash_before = stored_admin(fresh_db).password_hash

    admin_password("")
    ensure_admin_account(fresh_db)
    assert stored_admin(fresh_db).password_hash == hash_before


def test_unchanged_password_does_not_rehash_or_revoke(fresh_db, admin_password):
    from app.abs import sessions
    from app.services.users import ensure_admin_account

    admin_password("steady")
    admin = ensure_admin_account(fresh_db)
    hash_before = admin.password_hash
    sessions.create_ui(fresh_db, admin)

    ensure_admin_account(fresh_db)
    admin = stored_admin(fresh_db)
    # Same hash object, not merely an equivalent password: a rehash would
    # produce a different salt.
    assert admin.password_hash == hash_before
    assert len(sessions.active_for(fresh_db, admin)) == 1


def test_changed_password_is_stored_and_revokes_sessions(fresh_db, admin_password):
    from app.abs import sessions
    from app.passwords import verify_password
    from app.services.users import ensure_admin_account

    admin_password("old-password")
    admin = ensure_admin_account(fresh_db)
    sessions.create_ui(fresh_db, admin)
    assert sessions.active_for(fresh_db, admin)

    admin_password("new-password")
    ensure_admin_account(fresh_db)

    admin = stored_admin(fresh_db)
    assert verify_password("new-password", admin.password_hash)
    assert not verify_password("old-password", admin.password_hash)
    # Whoever knew the old password is signed out everywhere.
    assert sessions.active_for(fresh_db, admin) == []


def test_changing_the_password_re_enables_a_disabled_admin(fresh_db, admin_password):
    """ADMIN_PASSWORD is the way back in, so it cannot leave the account off."""
    from app.services.users import ensure_admin_account

    admin_password("before")
    admin = ensure_admin_account(fresh_db)
    admin.enabled = False
    fresh_db.commit()

    admin_password("after")
    assert ensure_admin_account(fresh_db).enabled


def test_a_non_admin_account_named_admin_stops_startup(fresh_db, admin_password):
    """Only reachable by hand, and the unique index would reject the insert
    anyway — but silently promoting a real user's row would be far worse."""
    from app.models import User
    from app.services.users import ensure_admin_account

    fresh_db.add(User(username="admin", password_hash=cheap_password_hash()))
    fresh_db.commit()

    admin_password("whatever")
    with pytest.raises(RuntimeError, match="rename"):
        ensure_admin_account(fresh_db)
    # ...and it certainly did not become the administrator.
    assert not stored_admin(fresh_db).is_admin


def test_a_lookalike_name_also_stops_startup(fresh_db, admin_password):
    """Usernames are case-insensitive, so "Admin" holds the name just as
    firmly — the insert would fail on the unique index either way."""
    from app.models import User
    from app.services.users import ensure_admin_account

    fresh_db.add(User(username="Admin", password_hash=cheap_password_hash()))
    fresh_db.commit()

    admin_password("whatever")
    with pytest.raises(RuntimeError, match="rename"):
        ensure_admin_account(fresh_db)


def test_the_account_is_found_by_role_not_by_name(fresh_db, admin_password):
    """Renaming the administrator in the database is not startup's business:
    the role is its identity, so it is reconciled, not duplicated."""
    from app.models import User, UserRole
    from app.passwords import verify_password
    from app.services.users import ensure_admin_account

    admin_password("original")
    admin = ensure_admin_account(fresh_db)
    admin.username = "sysop"
    fresh_db.commit()

    admin_password("rotated")
    again = ensure_admin_account(fresh_db)

    assert again.id == admin.id
    assert again.username == "sysop"
    # One administrator, and no second row created under the default name.
    assert fresh_db.scalars(select(User).where(User.role == UserRole.ADMIN)).all() == [again]
    assert fresh_db.scalar(select(User).where(User.username == "admin")) is None
    # The password change still applied to it.
    assert verify_password("rotated", again.password_hash)


def test_the_admin_is_excluded_from_hardcover_sync(fresh_db, admin_password):
    """The admin is a role, not a flag, precisely so the sync selects skip it
    without needing to know it exists."""
    from app.models import User, UserRole
    from app.services.users import ensure_admin_account

    admin_password("secret")
    admin = ensure_admin_account(fresh_db)
    # A token could only get there by hand, but the filter is what matters.
    admin.hardcover_token = "should-never-be-used"
    fresh_db.commit()

    syncable = fresh_db.scalars(
        select(User).where(
            User.enabled, User.role == UserRole.FULL, User.hardcover_token != ""
        )
    ).all()
    assert syncable == []
