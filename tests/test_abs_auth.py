import pytest

from app.abs import tokens


@pytest.fixture
def auth_enabled(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "auth_username", "dave")
    monkeypatch.setattr(test_settings, "auth_password", "hunter2")


def abs_login(client, username="dave", password="hunter2", return_tokens=True):
    headers = {"x-return-tokens": "true"} if return_tokens else {}
    return client.post(
        "/login", json={"username": username, "password": password}, headers=headers
    )


def test_status_public(client, auth_enabled):
    response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "audiobookshelf"
    assert body["isInit"] is True
    assert body["authMethods"] == ["local"]


def test_ping_public(client, auth_enabled):
    assert client.get("/ping").json() == {"success": True}
    assert client.get("/healthcheck").status_code == 200


def test_json_login_returns_tokens(client, auth_enabled):
    response = abs_login(client)
    assert response.status_code == 200
    body = response.json()
    user = body["user"]
    assert user["username"] == "dave"
    assert user["type"] == "root"
    assert user["accessToken"]
    assert user["refreshToken"]
    assert user["token"]  # legacy token
    assert user["permissions"]["download"] is True
    assert body["userDefaultLibraryId"] == "lib_audiobooks"
    assert body["serverSettings"]["version"] == "2.35.1"
    assert body["Source"] == "docker"

    payload = tokens.verify_token(user["accessToken"])
    assert payload["type"] == "access"
    assert payload["username"] == "dave"
    # legacy token: no expiry, still verifies
    assert tokens.verify_token(user["token"])["userId"] == payload["userId"]


def test_json_login_without_return_tokens_sets_cookie(client, auth_enabled):
    response = abs_login(client, return_tokens=False)
    assert response.status_code == 200
    assert response.json()["user"]["refreshToken"] is None
    assert "refresh_token" in response.cookies


def test_json_login_bad_credentials(client, auth_enabled):
    response = abs_login(client, password="wrong")
    assert response.status_code == 401
    assert "error" in response.json()


def test_form_login_still_works(client, auth_enabled):
    response = client.post(
        "/login",
        data={"username": "dave", "password": "hunter2", "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_authorize_with_bearer(client, auth_enabled):
    access = abs_login(client).json()["user"]["accessToken"]
    response = client.post("/api/authorize", headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "dave"


def test_api_requires_token(client, auth_enabled):
    assert client.post("/api/authorize").status_code == 401
    assert client.get("/api/me").status_code == 401
    assert (
        client.post("/api/authorize", headers={"Authorization": "Bearer junk"}).status_code
        == 401
    )


def test_token_via_query_param(client, auth_enabled):
    access = abs_login(client).json()["user"]["accessToken"]
    response = client.get("/api/me", params={"token": access})
    assert response.status_code == 200
    assert response.json()["mediaProgress"] == []


def test_refresh_token_rejected_as_access_token(client, auth_enabled):
    refresh = abs_login(client).json()["user"]["refreshToken"]
    response = client.get("/api/me", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_refresh_flow_with_header(client, auth_enabled):
    refresh = abs_login(client).json()["user"]["refreshToken"]
    response = client.post("/auth/refresh", headers={"x-refresh-token": refresh})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["accessToken"]
    assert body["user"]["refreshToken"]
    assert tokens.verify_token(body["user"]["accessToken"])["type"] == "access"


def test_refresh_requires_token(client, auth_enabled):
    assert client.post("/auth/refresh").status_code == 401


def test_access_token_rejected_as_refresh(client, auth_enabled):
    access = abs_login(client).json()["user"]["accessToken"]
    response = client.post("/auth/refresh", headers={"x-refresh-token": access})
    assert response.status_code == 401


def test_open_mode_allows_api_without_token(client, test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "auth_username", "")
    monkeypatch.setattr(test_settings, "auth_password", "")
    assert client.get("/api/me").status_code == 200
