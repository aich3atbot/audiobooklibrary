import pytest
from sqlalchemy import select

from app.models import Bookmark, DownloadState, Edition, User, UserRole
from tests.conftest import cheap_password_hash


@pytest.fixture
def other_user(db_session):
    existing = db_session.scalar(select(User).where(User.username == "erin"))
    if existing is not None:
        db_session.delete(existing)
        db_session.commit()
    account = User(username="erin", password_hash=cheap_password_hash("erin-pw"))
    db_session.add(account)
    db_session.commit()
    account_id = account.id
    yield account
    db_session.expire_all()
    leftover = db_session.get(User, account_id)
    if leftover is not None:
        db_session.delete(leftover)
        db_session.commit()


def test_users_page_lists_accounts(admin_client, user):
    page = admin_client.get("/admin/users")
    assert page.status_code == 200
    assert user.username in page.text
    assert "Add user" in page.text


def test_admin_routes_forbidden_without_admin(client, anon_client):
    # regular user: middleware redirects away
    assert client.get("/admin/users", follow_redirects=False).status_code == 303
    # anonymous: login redirect
    response = anon_client.get("/admin/users", follow_redirects=False)
    assert response.headers["location"].startswith("/login")


def test_create_user(admin_client, db_session):
    db_session.query(User).filter(User.username == "frank").delete()
    db_session.commit()

    response = admin_client.post(
        "/admin/users",
        data={"username": "frank", "password": "frank-pw", "hardcover_token": "tok"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    created = db_session.scalar(select(User).where(User.username == "frank"))
    assert created is not None
    assert created.enabled
    assert created.hardcover_token == "tok"
    assert created.password_hash.startswith("scrypt$")
    assert created.uuid

    # the new user can log in
    from fastapi.testclient import TestClient

    from app.main import app

    fresh = TestClient(app)
    login = fresh.post(
        "/login",
        data={"username": "frank", "password": "frank-pw"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    db_session.delete(created)
    db_session.commit()


def test_create_user_validation(admin_client, user):
    assert (
        admin_client.post("/admin/users", data={"username": "", "password": "x"}).status_code
        == 422
    )
    assert (
        admin_client.post("/admin/users", data={"username": "x", "password": ""}).status_code
        == 422
    )
    reserved = admin_client.post(
        "/admin/users", data={"username": "Admin", "password": "x"}
    )
    assert reserved.status_code == 422
    assert "reserved" in reserved.text
    duplicate = admin_client.post(
        "/admin/users", data={"username": user.username, "password": "x"}
    )
    assert duplicate.status_code == 422
    assert "already exists" in duplicate.text


def test_disable_enable_user(admin_client, other_user, db_session):
    response = admin_client.post(
        f"/admin/users/{other_user.id}/disable", follow_redirects=False
    )
    assert response.status_code == 303
    db_session.refresh(other_user)
    assert not other_user.enabled

    admin_client.post(f"/admin/users/{other_user.id}/enable", follow_redirects=False)
    db_session.refresh(other_user)
    assert other_user.enabled


def test_change_password(admin_client, other_user, db_session, anon_client):
    response = admin_client.post(
        f"/admin/users/{other_user.id}/password",
        data={"password": "new-pw"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    login = anon_client.post(
        "/login",
        data={"username": other_user.username, "password": "new-pw"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    empty = admin_client.post(
        f"/admin/users/{other_user.id}/password", data={"password": ""}
    )
    assert empty.status_code == 422


def test_change_password_revokes_abs_logins(admin_client, other_user, anon_client):
    """Taking an account back has to end the sessions opened with the old
    password — an ABS client would otherwise refresh for another 30 days."""
    refresh = anon_client.post(
        "/login",
        json={"username": other_user.username, "password": "erin-pw"},
        headers={"x-return-tokens": "true"},
    ).json()["user"]["refreshToken"]
    assert anon_client.post(
        "/auth/refresh", headers={"x-refresh-token": refresh}).status_code == 200

    admin_client.post(
        f"/admin/users/{other_user.id}/password", data={"password": "new-pw"}
    )
    assert anon_client.post(
        "/auth/refresh", headers={"x-refresh-token": refresh}).status_code == 401


def test_change_token(admin_client, other_user, db_session):
    response = admin_client.post(
        f"/admin/users/{other_user.id}/token",
        data={"hardcover_token": "  new-token  "},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(other_user)
    assert other_user.hardcover_token == "new-token"


def test_create_limited_user_gets_no_token(admin_client, db_session):
    """"Limited means no Hardcover" is an invariant: a token posted alongside
    the limited role is dropped, not stored."""
    db_session.query(User).filter(User.username == "gina").delete()
    db_session.commit()

    response = admin_client.post(
        "/admin/users",
        data={
            "username": "gina",
            "password": "gina-pw",
            "role": "limited",
            "hardcover_token": "tok",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    created = db_session.scalar(select(User).where(User.username == "gina"))
    assert created.role == UserRole.LIMITED
    assert created.hardcover_token == ""

    page = admin_client.get("/admin/users")
    assert "limited" in page.text

    db_session.delete(created)
    db_session.commit()


def test_change_role_clears_the_token_on_demotion(admin_client, other_user, db_session):
    other_user.hardcover_token = "tok"
    db_session.commit()

    response = admin_client.post(
        f"/admin/users/{other_user.id}/role",
        data={"role": "limited"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(other_user)
    assert other_user.role == UserRole.LIMITED
    assert other_user.hardcover_token == ""

    # promoting back leaves the token empty for the admin to set
    admin_client.post(f"/admin/users/{other_user.id}/role", data={"role": "full"})
    db_session.refresh(other_user)
    assert other_user.role == UserRole.FULL
    assert other_user.hardcover_token == ""


def test_token_change_refused_for_a_limited_user(admin_client, other_user, db_session):
    other_user.role = UserRole.LIMITED
    db_session.commit()

    response = admin_client.post(
        f"/admin/users/{other_user.id}/token", data={"hardcover_token": "tok"}
    )
    assert response.status_code == 422
    assert "no Hardcover access" in response.text
    db_session.refresh(other_user)
    assert other_user.hardcover_token == ""


def test_delete_user(admin_client, other_user, db_session):
    user_id = other_user.id
    response = admin_client.post(f"/admin/users/{user_id}/delete")
    assert response.status_code == 200
    assert f"Deleted user <strong>{other_user.username}</strong>" in response.text
    db_session.expire_all()
    assert db_session.get(User, user_id) is None


def test_actions_on_missing_user_404(admin_client):
    assert admin_client.post("/admin/users/99999/disable").status_code == 404
    assert admin_client.post("/admin/users/99999/delete").status_code == 404
    assert admin_client.get("/admin/users/99999/delete").status_code == 404


def test_the_admin_account_is_not_listed(admin_client, db_session):
    """It is a row now, but not an administrable one — and showing it would
    invite exactly the actions the next test refuses."""
    page = admin_client.get("/admin/users")
    assert page.status_code == 200
    assert "<td>admin</td>" not in page.text


def test_the_admin_account_cannot_be_administered(admin_client, db_session):
    """No verb may disable, delete, demote or re-password the admin: there is
    no second administrator to undo it, and ADMIN_PASSWORD owns the password."""
    admin = db_session.scalar(select(User).where(User.username == "admin"))
    base = f"/admin/users/{admin.id}"

    assert admin_client.post(f"{base}/disable").status_code == 404
    assert admin_client.post(f"{base}/enable").status_code == 404
    assert admin_client.post(f"{base}/password", data={"password": "x"}).status_code == 404
    assert admin_client.post(f"{base}/role", data={"role": "limited"}).status_code == 404
    assert admin_client.post(f"{base}/token", data={"hardcover_token": "t"}).status_code == 404
    assert admin_client.get(f"{base}/delete").status_code == 404
    assert admin_client.post(f"{base}/delete").status_code == 404

    db_session.refresh(admin)
    assert admin.enabled and admin.is_admin and admin.hardcover_token == ""


# --- delete-user orphan review ----------------------------------------------


@pytest.fixture
def orphan_library(db_session, other_user, user, test_settings):
    """other_user's library: one on-disk book only they have, one on-disk
    book shared with the default user, one metadata-only orphan."""
    import shutil

    from app.models import AudioFile, Author, Book, MediaProgress, Release, UserBook
    from tests.conftest import make_user_book

    for model in (UserBook, AudioFile, MediaProgress, Bookmark, Release, Edition, Book, Author):
        db_session.query(model).delete()
    db_session.commit()

    lib = test_settings.library_dir
    if lib.exists():
        shutil.rmtree(lib)

    author = Author(hardcover_id=500, name="Ryan Rimmel")

    solo_dir = lib / "Ryan Rimmel" / "Solo Book"
    solo_dir.mkdir(parents=True)
    (solo_dir / "book.m4b").write_bytes(b"audio")
    solo = Book(hardcover_id=1, title="Solo Book", author=author)
    solo.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                 library_path=str(solo_dir)))

    shared_dir = lib / "Ryan Rimmel" / "Shared Book"
    shared_dir.mkdir(parents=True)
    (shared_dir / "book.m4b").write_bytes(b"audio")
    shared = Book(hardcover_id=2, title="Shared Book", author=author)
    shared.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                   library_path=str(shared_dir)))

    meta_only = Book(hardcover_id=3, title="Metadata Only", author=author)

    db_session.add_all([solo, shared, meta_only])
    db_session.commit()
    make_user_book(db_session, other_user, solo)
    make_user_book(db_session, other_user, shared)
    make_user_book(db_session, other_user, meta_only)
    make_user_book(db_session, user, shared)  # shared with the default user
    return {"solo": solo, "shared": shared, "meta_only": meta_only,
            "solo_dir": solo_dir, "shared_dir": shared_dir}


def test_delete_review_lists_only_orphans(admin_client, other_user, orphan_library):
    page = admin_client.get(f"/admin/users/{other_user.id}/delete")
    assert page.status_code == 200
    assert "Solo Book" in page.text
    assert "Shared Book" not in page.text  # another user has it
    assert "1 tracked book(s) without files" in page.text


def test_delete_user_removes_chosen_books_from_disk(
    admin_client, other_user, orphan_library, db_session
):
    from app.models import Book

    solo_id = orphan_library["solo"].id
    other_id = other_user.id
    response = admin_client.post(
        f"/admin/users/{other_id}/delete",
        data={f"disk_{solo_id}": "delete"},
    )
    assert response.status_code == 200
    assert "1 book(s) deleted from disk" in response.text
    assert not orphan_library["solo_dir"].exists()
    db_session.expire_all()
    assert db_session.get(User, other_id) is None
    assert db_session.get(Book, solo_id) is None
    # metadata-only orphan removed too; shared book untouched
    assert db_session.query(Book).filter_by(hardcover_id=3).count() == 0
    assert orphan_library["shared_dir"].exists()
    assert db_session.query(Book).filter_by(hardcover_id=2).count() == 1


def test_delete_user_leaves_books_in_place_by_default(
    admin_client, other_user, orphan_library, db_session, client
):
    from app.models import Book

    other_id = other_user.id
    response = admin_client.post(f"/admin/users/{other_id}/delete", data={})
    assert response.status_code == 200
    assert "1 left in place" in response.text
    assert orphan_library["solo_dir"].exists()
    db_session.expire_all()
    assert db_session.get(User, other_id) is None
    # the book survives as ownerless "available"
    solo = db_session.query(Book).filter_by(hardcover_id=1).one()
    assert solo.editions[0].library_path is not None
    assert solo.user_books == []


def test_delete_review_warns_about_other_users_progress(
    admin_client, other_user, user, orphan_library, db_session
):
    from app.models import MediaProgress

    db_session.add(MediaProgress(user_id=user.id,
                                 edition_id=orphan_library["solo"].editions[0].id,
                                 current_time=10.0, duration=100.0))
    db_session.commit()

    page = admin_client.get(f"/admin/users/{other_user.id}/delete")
    assert "others have progress" in page.text
