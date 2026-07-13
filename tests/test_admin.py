import pytest
from sqlalchemy import select

from app.models import User
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


def test_change_token(admin_client, other_user, db_session):
    response = admin_client.post(
        f"/admin/users/{other_user.id}/token",
        data={"hardcover_token": "  new-token  "},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(other_user)
    assert other_user.hardcover_token == "new-token"


def test_delete_user(admin_client, other_user, db_session):
    user_id = other_user.id
    response = admin_client.post(
        f"/admin/users/{user_id}/delete", follow_redirects=False
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.get(User, user_id) is None


def test_actions_on_missing_user_404(admin_client):
    assert admin_client.post("/admin/users/99999/disable").status_code == 404
    assert admin_client.post("/admin/users/99999/delete").status_code == 404
