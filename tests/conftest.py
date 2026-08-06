import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

ADMIN_PASSWORD = "admin-pw"
TEST_USERNAME = "dave"
TEST_PASSWORD = "hunter2"

_cheap_hash: str | None = None


def cheap_password_hash(password: str = TEST_PASSWORD) -> str:
    """A scrypt hash with cheap parameters (the format embeds them, so
    verification just works) — production cost would slow every test login."""
    global _cheap_hash
    if password != TEST_PASSWORD:
        from app.passwords import hash_password

        return hash_password(password, n=2**4)
    if _cheap_hash is None:
        from app.passwords import hash_password

        _cheap_hash = hash_password(TEST_PASSWORD, n=2**4)
    return _cheap_hash


@pytest.fixture(scope="session", autouse=True)
def test_settings(tmp_path_factory):
    """Point all paths at temp dirs before the app is imported anywhere, and
    pin the indexer/download-client config to inert defaults so the
    developer's real .env never leaks into tests."""
    root = tmp_path_factory.mktemp("abl")
    os.environ["CONFIG_DIR"] = str(root / "config")
    os.environ["DOWNLOAD_DIR"] = str(root / "downloads")
    os.environ["LIBRARY_DIR"] = str(root / "audiobooks")
    os.environ["IMPORTS_DIR"] = str(root / "imports")
    os.environ["ADMIN_PASSWORD"] = ADMIN_PASSWORD
    os.environ["INDEX_URL"] = ""
    os.environ["DOWNLOAD_CLIENT"] = "deluge"
    # Downloads count as enabled only with both CLIENT and URL set; a fake URL
    # keeps the baseline "enabled" (respx blocks any real request anyway).
    os.environ["DOWNLOAD_URL"] = "http://deluge.test:8112"
    os.environ["DOWNLOAD_USERNAME"] = ""
    os.environ["DOWNLOAD_PASSWORD"] = ""
    os.environ["DOWNLOAD_LABEL"] = ""
    os.environ["DOWNLOAD_REMOVE_IMMEDIATELY"] = "false"
    os.environ["IMPORT_MODE"] = "copy"

    from app.config import get_settings
    from app.db import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    from app.models import Base

    Base.metadata.create_all(get_engine())
    yield get_settings()


@pytest.fixture(autouse=True)
def no_real_http():
    """No test reaches the real network. Everything outbound is mocked with
    respx; this makes the *absence* of a mock an error instead of a live
    request — a progress sync that pushes to Hardcover swallows the failure and
    retries later, so an unmocked call would otherwise pass silently while
    POSTing to api.hardcover.app for real.

    Tests that mock with `@respx.mock` nest inside this router harmlessly, and
    routes registered without the decorator get rolled back here on teardown.
    The TestClient's ASGI transport is not intercepted."""
    import respx

    with respx.mock:
        yield


@pytest.fixture
def db_session(test_settings):
    from app.db import get_sessionmaker

    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def user(db_session):
    """The default regular account; get-or-create so it survives shared use
    across tests (the session-scoped DB persists)."""
    from sqlalchemy import select

    from app.models import User, UserRole

    existing = db_session.scalar(select(User).where(User.username == TEST_USERNAME))
    if existing is not None:
        if not existing.enabled or existing.is_limited:
            existing.enabled = True
            existing.role = UserRole.FULL
            db_session.commit()
        return existing
    account = User(
        username=TEST_USERNAME,
        password_hash=cheap_password_hash(),
        hardcover_token="hc-token",
    )
    db_session.add(account)
    db_session.commit()
    return account


@pytest.fixture
def tokenless_user(user, db_session):
    """The default user with no Hardcover token (push/sync disabled)."""
    user.hardcover_token = ""
    db_session.commit()
    yield user
    user.hardcover_token = "hc-token"
    db_session.commit()


@pytest.fixture
def limited_user(user, db_session):
    """The default user demoted to a limited (ABS-only) account: no web UI,
    no Hardcover token."""
    from app.models import UserRole

    user.role = UserRole.LIMITED
    user.hardcover_token = ""
    db_session.commit()
    yield user
    user.role = UserRole.FULL
    user.hardcover_token = "hc-token"
    db_session.commit()


def make_user_book(db, user, book, **kwargs):
    from app.models import UserBook

    user_book = UserBook(user_id=user.id, book=book, **kwargs)
    db.add(user_book)
    db.commit()
    return user_book


@pytest.fixture
def anon_client(test_settings):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def client(test_settings, user):
    """A TestClient logged in as the default user (auth is mandatory)."""
    from fastapi.testclient import TestClient

    from app.main import app

    logged_in = TestClient(app)
    response = logged_in.post(
        "/login",
        data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, "test login failed"
    return logged_in


@pytest.fixture
def admin_client(test_settings):
    from fastapi.testclient import TestClient

    from app.main import app

    admin = TestClient(app)
    response = admin.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, "admin login failed"
    return admin
