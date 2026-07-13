from app.abs import tokens
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


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
    assert payload_user["type"] == "root"
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
