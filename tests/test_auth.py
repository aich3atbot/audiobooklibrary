from tests.conftest import ADMIN_PASSWORD, TEST_PASSWORD, TEST_USERNAME


def login(client, username=TEST_USERNAME, password=TEST_PASSWORD, next="/"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": next},
        follow_redirects=False,
    )


def test_unauthenticated_requests_redirect_to_login(anon_client):
    response = anon_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"

    response = anon_client.get("/activity", follow_redirects=False)
    assert response.headers["location"] == "/login?next=/activity"


def test_query_string_preserved_in_next(anon_client):
    response = anon_client.get("/", params={"q": "noobtown"}, follow_redirects=False)
    assert response.headers["location"] == "/login?next=/?q=noobtown"


def test_healthz_and_static_stay_open(anon_client):
    assert anon_client.get("/healthz").status_code == 200
    assert anon_client.get("/static/app.css").status_code == 200


def test_login_page_renders(anon_client):
    response = anon_client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text


def test_wrong_credentials_rejected(anon_client, user):
    response = login(anon_client, password="wrong")
    assert response.status_code == 401
    assert "Invalid username or password" in response.text
    # still locked out
    assert anon_client.get("/", follow_redirects=False).status_code == 303


def test_unknown_user_rejected(anon_client):
    response = login(anon_client, username="nobody")
    assert response.status_code == 401


def test_login_grants_session(anon_client, user):
    response = login(anon_client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    page = anon_client.get("/")
    assert page.status_code == 200
    assert "Log out" in page.text
    assert TEST_USERNAME in page.text


def test_login_redirects_to_next(anon_client, user):
    response = login(anon_client, next="/activity")
    assert response.headers["location"] == "/activity"


def test_open_redirect_rejected(anon_client, user):
    response = login(anon_client, next="//evil.example.com/phish")
    assert response.headers["location"] == "/"


def test_logout_clears_session(client):
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_htmx_request_gets_hx_redirect(anon_client):
    response = anon_client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_login_page_redirects_home_when_already_logged_in(client):
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_disabled_user_cannot_log_in(anon_client, user, db_session):
    user.enabled = False
    db_session.commit()
    try:
        response = login(anon_client)
        assert response.status_code == 401
    finally:
        user.enabled = True
        db_session.commit()


def test_disabling_user_kills_existing_session(client, user, db_session):
    assert client.get("/", follow_redirects=False).status_code == 200
    user.enabled = False
    db_session.commit()
    try:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
    finally:
        user.enabled = True
        db_session.commit()


def test_admin_login_lands_on_user_admin(anon_client):
    response = login(anon_client, username="admin", password=ADMIN_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"


def test_wrong_admin_password_rejected(anon_client):
    response = login(anon_client, username="admin", password="wrong")
    assert response.status_code == 401


def test_admin_is_redirected_away_from_regular_routes(admin_client):
    response = admin_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"

    page = admin_client.get("/admin/users")
    assert page.status_code == 200
    assert "Add user" in page.text


def test_regular_user_is_redirected_away_from_admin(client):
    response = client.get("/admin/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_admin_login_page_redirects_to_admin(admin_client):
    response = admin_client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"
