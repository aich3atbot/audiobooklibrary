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


def test_limited_user_cannot_log_in_to_the_ui(anon_client, limited_user):
    """The password is right; this door just isn't theirs."""
    response = login(anon_client)
    assert response.status_code == 403
    assert "Audiobookshelf app" in response.text
    assert anon_client.get("/", follow_redirects=False).status_code == 303


def test_demoting_a_user_kills_their_browser_session(client, user, db_session):
    from app.models import UserRole

    assert client.get("/", follow_redirects=False).status_code == 200
    user.role = UserRole.LIMITED
    db_session.commit()
    try:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        # told why, and no ?next= — coming back would only bounce them again
        assert response.headers["location"] == "/login?error=app_only"
        page = client.get(response.headers["location"])
        assert "Audiobookshelf app" in page.text
    finally:
        user.role = UserRole.FULL
        db_session.commit()


def test_admin_login_lands_on_user_admin(anon_client):
    response = login(anon_client, username="admin", password=ADMIN_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"


def test_wrong_admin_password_rejected(anon_client):
    response = login(anon_client, username="admin", password="wrong")
    assert response.status_code == 401


def test_login_routes_on_the_role_not_the_username(anon_client, db_session):
    """Which interface you get is decided by `user.is_admin` alone — the login
    path never compares a name (or ADMIN_PASSWORD) against anything."""
    from sqlalchemy import select

    from app.models import User

    admin = db_session.scalar(select(User).where(User.username == "admin"))
    admin.username = "sysop"
    db_session.commit()
    try:
        response = login(anon_client, username="sysop", password=ADMIN_PASSWORD)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users"
        assert anon_client.get("/admin/users").status_code == 200
        # ...and the old name is now just a name nobody holds.
        assert login(anon_client, username="admin", password=ADMIN_PASSWORD).status_code == 401
    finally:
        db_session.refresh(admin)
        admin.username = "admin"
        db_session.commit()


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


# --- browser sessions are rows, and rows can be revoked ---------------------


def utcnow():
    """Naive UTC, matching how the session rows store their timestamps."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def ui_sessions(db, user):
    from app.models import AuthSession

    return (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user.id, AuthSession.kind == "ui")
        .all()
    )


def test_login_records_a_session_row(anon_client, user, db_session):
    before = len(ui_sessions(db_session, user))
    login(anon_client)

    rows = ui_sessions(db_session, user)
    assert len(rows) == before + 1
    row = rows[-1]
    # The cookie carries the token, never the user id; the row carries the
    # device details the ABS clients list.
    assert row.token_hash and row.last_token_hash is None
    assert row.user_agent
    assert row.ip_address


def test_revoking_the_row_logs_the_browser_out(client, user, db_session):
    assert client.get("/", follow_redirects=False).status_code == 200

    for row in ui_sessions(db_session, user):
        db_session.delete(row)
    db_session.commit()

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_logout_deletes_the_row(client, user, db_session):
    assert ui_sessions(db_session, user)
    client.post("/logout", follow_redirects=False)
    assert ui_sessions(db_session, user) == []


def test_logout_all_devices_revokes_every_session(client, user, db_session):
    from app.abs import sessions

    sessions.create_ui(db_session, user)  # a second browser
    # (the shared account accumulates logins across the suite; what matters is
    # that this one call takes them all)
    assert len(sessions.active_for(db_session, user)) >= 2

    client.post("/logout?allDevices=1", follow_redirects=False)
    assert sessions.active_for(db_session, user) == []


def test_expired_session_is_rejected(client, user, db_session):
    from datetime import timedelta

    rows = ui_sessions(db_session, user)
    for row in rows:
        row.expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert client.get("/", follow_redirects=False).status_code == 303


def test_using_a_session_slides_its_expiry(client, user, db_session):
    from datetime import timedelta

    row = ui_sessions(db_session, user)[-1]
    # Idle long enough to earn a touch, and close enough to expiry to notice.
    row.updated_at = utcnow() - timedelta(hours=2)
    row.expires_at = utcnow() + timedelta(days=1)
    db_session.commit()

    assert client.get("/", follow_redirects=False).status_code == 200

    db_session.refresh(row)
    assert row.expires_at > utcnow() + timedelta(days=29)


def test_a_recently_used_session_is_not_rewritten(client, user, db_session):
    """The touch is throttled: an ordinary page view stays a pure read."""
    row = ui_sessions(db_session, user)[-1]
    updated_before = row.updated_at
    expires_before = row.expires_at

    assert client.get("/", follow_redirects=False).status_code == 200

    db_session.refresh(row)
    assert row.updated_at == updated_before
    assert row.expires_at == expires_before


def test_password_change_logs_the_user_out_of_the_browser(
    client, admin_client, user, db_session
):
    """A reset takes a compromised account back — including whatever browser
    the old password is signed in on."""
    assert client.get("/", follow_redirects=False).status_code == 200
    original_hash = user.password_hash

    try:
        response = admin_client.post(
            f"/admin/users/{user.id}/password",
            data={"password": "brand-new"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        assert ui_sessions(db_session, user) == []
        assert client.get("/", follow_redirects=False).status_code == 303
    finally:
        # The default account is shared across the suite; give it its cheap
        # hash back or every later login fails. refresh() first: the route
        # wrote through its own session, so assigning the old value to this
        # stale copy would look like no change at all and emit no UPDATE.
        db_session.refresh(user)
        user.password_hash = original_hash
        db_session.commit()


def test_an_app_can_sign_the_browser_out(client, anon_client, user, db_session):
    """Browser sessions share the table the ABS device list reads, so a phone
    can revoke a laptop — which is how upstream behaves."""
    assert client.get("/", follow_redirects=False).status_code == 200

    app_login = anon_client.post(
        "/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        headers={"x-return-tokens": "true"},
    ).json()["user"]
    headers = {
        "Authorization": f"Bearer {app_login['accessToken']}",
        "x-refresh-token": app_login["refreshToken"],
    }

    listed = anon_client.get("/api/me/sessions", headers=headers).json()["sessions"]
    browser_ids = {row.uuid for row in ui_sessions(db_session, user)}
    listed_browsers = [s for s in listed if s["id"] in browser_ids]
    assert listed_browsers, "the browser session should appear in the device list"
    # The app's own session is the current one; a browser never is.
    assert all(not s["current"] for s in listed_browsers)

    for row in listed_browsers:
        assert (
            anon_client.delete(f"/api/me/sessions/{row['id']}", headers=headers).status_code
            == 200
        )

    assert client.get("/", follow_redirects=False).status_code == 303


def test_admin_session_is_a_row_too(admin_client, db_session):
    """The whole point of the admin being a row: its browser session can be
    revoked like anyone else's."""
    from sqlalchemy import select

    from app.models import User

    admin = db_session.scalar(select(User).where(User.username == "admin"))
    rows = ui_sessions(db_session, admin)
    assert rows

    for row in rows:
        db_session.delete(row)
    db_session.commit()

    response = admin_client.get("/admin/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
