import pytest

from app.abs import tokens
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


@pytest.fixture(autouse=True)
def clean_sessions(db_session):
    """Logins persist an auth_session row, and the test database is shared
    across the run — start every test in this module with none."""
    from app.models import AuthSession

    db_session.query(AuthSession).delete()
    db_session.commit()


def abs_login(client, username=TEST_USERNAME, password=TEST_PASSWORD, return_tokens=True):
    headers = {"x-return-tokens": "true"} if return_tokens else {}
    return client.post(
        "/login", json={"username": username, "password": password}, headers=headers
    )


def test_status_public(anon_client):
    response = anon_client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "audiobookshelf"
    assert body["isInit"] is True
    assert body["authMethods"] == ["local"]


def test_ping_public(anon_client):
    assert anon_client.get("/ping").json() == {"success": True}
    assert anon_client.get("/healthcheck").status_code == 200


def test_json_login_returns_tokens(anon_client, user):
    response = abs_login(anon_client)
    assert response.status_code == 200
    body = response.json()
    payload_user = body["user"]
    assert payload_user["username"] == TEST_USERNAME
    assert payload_user["id"] == user.uuid
    # Plain "user": root/admin would unlock client server-admin UI we don't serve.
    assert payload_user["type"] == "user"
    assert payload_user["accessToken"]
    assert payload_user["refreshToken"]
    assert payload_user["token"]  # legacy token
    assert payload_user["permissions"]["download"] is True
    assert body["userDefaultLibraryId"] == "lib_audiobooks"
    assert body["serverSettings"]["version"] == "2.35.1"
    assert body["Source"] == "docker"

    payload = tokens.verify_token(payload_user["accessToken"])
    assert payload["type"] == "access"
    assert payload["username"] == TEST_USERNAME
    assert payload["userId"] == user.uuid
    # legacy token: no expiry, still verifies
    assert tokens.verify_token(payload_user["token"])["userId"] == user.uuid


def test_json_login_without_return_tokens_sets_cookie(anon_client, user):
    response = abs_login(anon_client, return_tokens=False)
    assert response.status_code == 200
    assert response.json()["user"]["refreshToken"] is None
    assert "refresh_token" in response.cookies


def test_json_login_bad_credentials(anon_client, user):
    response = abs_login(anon_client, password="wrong")
    assert response.status_code == 401
    assert "error" in response.json()


def test_json_login_admin_rejected(anon_client):
    from tests.conftest import ADMIN_PASSWORD

    response = abs_login(anon_client, username="admin", password=ADMIN_PASSWORD)
    assert response.status_code == 401


def test_a_token_for_the_admin_is_worthless(anon_client, db_session):
    """No login mints one, but the admin has a uuid like any other row now, so
    the API refuses tokens that name it rather than trusting that."""
    from sqlalchemy import select

    from app.models import User

    admin = db_session.scalar(select(User).where(User.username == "admin"))
    access = tokens.create_access_token(admin)
    refresh = tokens.create_refresh_token(admin)

    assert anon_client.get(
        "/api/me", headers={"Authorization": f"Bearer {access}"}
    ).status_code == 401
    assert anon_client.post(
        "/auth/refresh", headers={"x-refresh-token": refresh}
    ).status_code == 401


def test_limited_user_logs_in_like_any_other(anon_client, limited_user):
    """The whole point of a limited account: the ABS surface is unchanged for
    it, only the web UI and Hardcover are gone."""
    response = abs_login(anon_client)
    assert response.status_code == 200
    payload_user = response.json()["user"]
    assert payload_user["id"] == limited_user.uuid
    assert payload_user["type"] == "user"
    assert payload_user["accessToken"]
    assert payload_user["refreshToken"]

    access = payload_user["accessToken"]
    headers = {"Authorization": f"Bearer {access}"}
    assert anon_client.get("/api/me", headers=headers).status_code == 200
    assert anon_client.post("/api/authorize", headers=headers).status_code == 200


def test_form_login_still_works(anon_client, user):
    response = anon_client.post(
        "/login",
        data={"username": TEST_USERNAME, "password": TEST_PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_authorize_with_bearer(anon_client, user):
    access = abs_login(anon_client).json()["user"]["accessToken"]
    response = anon_client.post(
        "/api/authorize", headers={"Authorization": f"Bearer {access}"}
    )
    assert response.status_code == 200
    assert response.json()["user"]["username"] == TEST_USERNAME


def test_api_requires_token(anon_client, user):
    assert anon_client.post("/api/authorize").status_code == 401
    assert anon_client.get("/api/me").status_code == 401
    assert (
        anon_client.post(
            "/api/authorize", headers={"Authorization": "Bearer junk"}
        ).status_code
        == 401
    )


def test_token_via_query_param(anon_client, user):
    access = abs_login(anon_client).json()["user"]["accessToken"]
    response = anon_client.get("/api/me", params={"token": access})
    assert response.status_code == 200
    assert response.json()["mediaProgress"] == []


def test_refresh_token_rejected_as_access_token(anon_client, user):
    refresh = abs_login(anon_client).json()["user"]["refreshToken"]
    response = anon_client.get("/api/me", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_refresh_flow_with_header(anon_client, user):
    refresh = abs_login(anon_client).json()["user"]["refreshToken"]
    response = anon_client.post("/auth/refresh", headers={"x-refresh-token": refresh})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["accessToken"]
    assert body["user"]["refreshToken"]
    assert tokens.verify_token(body["user"]["accessToken"])["type"] == "access"


def test_refresh_requires_token(anon_client, user):
    assert anon_client.post("/auth/refresh").status_code == 401


def test_access_token_rejected_as_refresh(anon_client, user):
    access = abs_login(anon_client).json()["user"]["accessToken"]
    response = anon_client.post("/auth/refresh", headers={"x-refresh-token": access})
    assert response.status_code == 401


def test_disabled_user_token_rejected(anon_client, user, db_session):
    access = abs_login(anon_client).json()["user"]["accessToken"]
    user.enabled = False
    db_session.commit()
    try:
        response = anon_client.get(
            "/api/me", headers={"Authorization": f"Bearer {access}"}
        )
        assert response.status_code == 401
    finally:
        user.enabled = True
        db_session.commit()


# --- sessions: logout has to actually revoke --------------------------------


def refresh_with(client, token):
    return client.post("/auth/refresh", headers={"x-refresh-token": token})


def test_logout_revokes_the_refresh_token(anon_client, user):
    refresh = abs_login(anon_client).json()["user"]["refreshToken"]
    assert refresh_with(anon_client, refresh).status_code == 200

    response = anon_client.post("/logout", headers={"x-refresh-token": refresh},
                                follow_redirects=False)
    assert response.status_code == 200
    assert response.json()["success"] is True

    # the JWT still verifies on its own — the session behind it is gone
    assert tokens.verify_token(refresh) is not None
    assert refresh_with(anon_client, refresh).status_code == 401


def test_logout_only_signs_out_that_device(anon_client, user):
    phone = abs_login(anon_client).json()["user"]["refreshToken"]
    tablet = abs_login(anon_client).json()["user"]["refreshToken"]

    anon_client.post("/logout", headers={"x-refresh-token": phone})
    assert refresh_with(anon_client, phone).status_code == 401
    assert refresh_with(anon_client, tablet).status_code == 200


def test_logout_all_devices(anon_client, user):
    phone = abs_login(anon_client).json()["user"]["refreshToken"]
    tablet = abs_login(anon_client).json()["user"]["refreshToken"]

    response = anon_client.post("/logout?allDevices=1",
                                headers={"x-refresh-token": phone})
    assert response.status_code == 200
    assert refresh_with(anon_client, phone).status_code == 401
    assert refresh_with(anon_client, tablet).status_code == 401


def test_ui_logout_still_gets_the_login_page(client):
    """Both callers share the route: the browser form must get a redirect, not
    the JSON an ABS client asks for. (test_auth covers the session clearing.)"""
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_refresh_token_without_a_session_is_rejected(anon_client, user):
    """A token minted outside a login — including one issued before sessions
    existed — has nothing to refresh against."""
    orphan = tokens.create_refresh_token(user)
    assert refresh_with(anon_client, orphan).status_code == 401


def test_rotation_invalidates_the_old_token_after_the_grace_period(
    anon_client, user, db_session
):
    from datetime import timedelta

    from app.abs import sessions
    from app.models import AuthSession

    first = abs_login(anon_client).json()["user"]["refreshToken"]
    second = refresh_with(anon_client, first).json()["user"]["refreshToken"]
    assert second != first

    # Inside the window the superseded token still works, but does not rotate
    # again — the client keeps the token it already holds, and the token that
    # won the race stays current.
    response = refresh_with(anon_client, first)
    assert response.status_code == 200
    assert response.json()["user"]["accessToken"]
    assert response.json()["user"]["refreshToken"] is None

    session = db_session.query(AuthSession).filter_by(
        last_token_hash=sessions.token_hash(first)).one()
    assert session.token_hash == sessions.token_hash(second)

    session.last_token_expires_at = sessions._utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert refresh_with(anon_client, first).status_code == 401
    assert refresh_with(anon_client, second).status_code == 200


def test_expired_session_is_rejected_and_cleaned_up(anon_client, user, db_session):
    from datetime import timedelta

    from app.abs import sessions
    from app.models import AuthSession

    refresh = abs_login(anon_client).json()["user"]["refreshToken"]
    session = db_session.query(AuthSession).filter_by(
        token_hash=sessions.token_hash(refresh)).one()
    session.expires_at = sessions._utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert refresh_with(anon_client, refresh).status_code == 401
    db_session.expire_all()
    assert db_session.query(AuthSession).filter_by(
        token_hash=sessions.token_hash(refresh)).count() == 0


def test_list_and_revoke_sessions(anon_client, user):
    phone = abs_login(anon_client).json()["user"]["refreshToken"]
    login = abs_login(anon_client).json()["user"]
    tablet, access = login["refreshToken"], login["accessToken"]
    headers = {"Authorization": f"Bearer {access}", "x-refresh-token": tablet}

    body = anon_client.get("/api/me/sessions", headers=headers).json()
    assert body["total"] == 2
    assert body["numPages"] == 1
    assert [s["current"] for s in body["sessions"]].count(True) == 1

    other = next(s for s in body["sessions"] if not s["current"])
    assert anon_client.delete(f"/api/me/sessions/{other['id']}",
                              headers=headers).status_code == 200
    assert refresh_with(anon_client, phone).status_code == 401
    assert refresh_with(anon_client, tablet).status_code == 200

    # non-uuid ids are a 400, someone else's (or a gone) session a 404
    assert anon_client.delete("/api/me/sessions/nope", headers=headers).status_code == 400
    assert anon_client.delete(f"/api/me/sessions/{other['id']}",
                              headers=headers).status_code == 404


def test_sessions_are_per_user(anon_client, user, db_session):
    from app.models import User
    from tests.conftest import cheap_password_hash

    other = db_session.query(User).filter_by(username="pat").one_or_none()
    if other is None:
        other = User(username="pat", password_hash=cheap_password_hash())
        db_session.add(other)
        db_session.commit()

    mine = abs_login(anon_client).json()["user"]
    theirs = abs_login(anon_client, username="pat", password="hunter2").json()["user"]

    body = anon_client.get(
        "/api/me/sessions",
        headers={"Authorization": f"Bearer {mine['accessToken']}",
                 "x-refresh-token": mine["refreshToken"]},
    ).json()
    assert body["total"] == 1

    # and one user cannot revoke another's device
    their_id = anon_client.get(
        "/api/me/sessions",
        headers={"Authorization": f"Bearer {theirs['accessToken']}",
                 "x-refresh-token": theirs["refreshToken"]},
    ).json()["sessions"][0]["id"]
    assert anon_client.delete(
        f"/api/me/sessions/{their_id}",
        headers={"Authorization": f"Bearer {mine['accessToken']}"},
    ).status_code == 404
