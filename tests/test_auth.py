import pytest


@pytest.fixture
def auth_enabled(test_settings, monkeypatch):
    # patch the cached settings instance: pydantic stores field values in
    # instance __dict__, so class-level patches are shadowed
    monkeypatch.setattr(test_settings, "auth_username", "dave")
    monkeypatch.setattr(test_settings, "auth_password", "hunter2")


def login(client, username="dave", password="hunter2", next="/"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": next},
        follow_redirects=False,
    )


def test_no_auth_configured_leaves_app_open(client, test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "auth_username", "")
    monkeypatch.setattr(test_settings, "auth_password", "")
    assert client.get("/healthz").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200


def test_unauthenticated_requests_redirect_to_login(client, auth_enabled):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"

    response = client.get("/activity", follow_redirects=False)
    assert response.headers["location"] == "/login?next=/activity"


def test_query_string_preserved_in_next(client, auth_enabled):
    response = client.get("/", params={"q": "noobtown"}, follow_redirects=False)
    assert response.headers["location"] == "/login?next=/?q=noobtown"


def test_healthz_and_static_stay_open(client, auth_enabled):
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_login_page_renders(client, auth_enabled):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text


def test_wrong_credentials_rejected(client, auth_enabled):
    response = login(client, password="wrong")
    assert response.status_code == 401
    assert "Invalid username or password" in response.text
    # still locked out
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_grants_session(client, auth_enabled):
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    page = client.get("/")
    assert page.status_code == 200
    assert "Log out" in page.text


def test_login_redirects_to_next(client, auth_enabled):
    response = login(client, next="/activity")
    assert response.headers["location"] == "/activity"


def test_open_redirect_rejected(client, auth_enabled):
    response = login(client, next="//evil.example.com/phish")
    assert response.headers["location"] == "/"


def test_logout_clears_session(client, auth_enabled):
    login(client)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_htmx_request_gets_hx_redirect(client, auth_enabled):
    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_login_page_redirects_home_when_already_logged_in(client, auth_enabled):
    login(client)
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
