"""Multi-user session authentication.

Accounts are mandatory: every UI route requires a logged-in user (or the
virtual admin, who only sees user administration). The login session lives
in a signed cookie (Starlette SessionMiddleware); regular users are
re-checked against the database on every request so disabling an account
locks it out immediately.

*Limited* accounts (`UserRole.LIMITED`) are ABS-only: they authenticate over
the API surface (`/api/`, `/auth/`) but never here. The per-request re-check
is what enforces that — a user demoted mid-session loses the UI on their very
next request, which matters because browser sessions are signed cookies with
no server-side row to revoke.
"""

import secrets

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import get_settings
from app.db import get_db, get_sessionmaker
from app.models import User

SESSION_USER_ID = "user_id"
SESSION_IS_ADMIN = "is_admin"
ADMIN_USERNAME = "admin"
# /status, /ping, /healthcheck: ABS client discovery must be public.
# /logout too: ABS clients post it with a bearer/refresh token and no cookie,
# and a redirect to /login would hand them an HTML page instead of revoking.
# It clears whatever session the request carries, so anonymous posts are inert.
OPEN_PATHS = {"/login", "/logout", "/healthz", "/status", "/ping", "/healthcheck"}
# /api/ and /auth/ are the ABS surface: bearer-token auth enforced per-route
# (app/abs/deps.py), not by this cookie-redirect middleware. /public/ is the
# ABS surface that carries no token at all (app/abs/public_routes.py) — a
# redirect to /login there feeds an HTML page to the app's audio player.
OPEN_PREFIXES = ("/static/", "/api/", "/auth/", "/public/")

# Query key on /login explaining why the UI turned an account away. A key
# rather than a message so nothing arbitrary reaches the template.
APP_ONLY_ERROR = "app_only"
APP_ONLY_MESSAGE = "This account can only be used with an Audiobookshelf app."


def resolve_session_secret() -> str:
    """Generate the cookie-signing secret once and persist it in the config
    dir so sessions survive restarts."""
    settings = get_settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    path = settings.config_dir / "session_secret"
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_hex(32)
    path.write_text(secret)
    path.chmod(0o600)
    return secret


def check_admin_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    # compare_digest on both fields to avoid timing side channels
    user_ok = secrets.compare_digest(username.encode(), ADMIN_USERNAME.encode())
    pass_ok = secrets.compare_digest(password.encode(), settings.admin_password.encode())
    return user_ok and pass_ok


def safe_next(next_url: str) -> str:
    """Only allow same-site relative redirect targets."""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """The logged-in regular user. The middleware guarantees one on UI
    routes; re-queried here so it is attached to the route's own session."""
    user_id = request.session.get(SESSION_USER_ID)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None or not user.enabled:
        raise HTTPException(status_code=401, detail="Not logged in")
    # The middleware already turned limited accounts away; this is the
    # backstop for any UI route reached without passing through it.
    if user.is_limited:
        raise HTTPException(status_code=403, detail=APP_ONLY_MESSAGE)
    return user


def require_admin(request: Request) -> None:
    if not request.session.get(SESSION_IS_ADMIN):
        raise HTTPException(status_code=403, detail="Admin only")


class RequireAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.username = None
        request.state.is_admin = False
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)

        if request.session.get(SESSION_IS_ADMIN):
            request.state.username = ADMIN_USERNAME
            request.state.is_admin = True
            # The admin account only administers users.
            if not path.startswith("/admin") and path != "/logout":
                return RedirectResponse(url="/admin/users", status_code=303)
            return await call_next(request)

        limited = False
        user_id = request.session.get(SESSION_USER_ID)
        if user_id is not None:
            with get_sessionmaker()() as db:
                user = db.get(User, user_id)
            if user is not None and user.enabled and not user.is_limited:
                request.state.user_id = user.id
                request.state.username = user.username
                if path.startswith("/admin"):
                    return RedirectResponse(url="/", status_code=303)
                return await call_next(request)
            # Deleted, disabled or demoted mid-session: drop the stale session.
            limited = user is not None and user.is_limited
            request.session.clear()

        # HTMX fragment requests can't render a redirect target themselves;
        # tell htmx to do a full-page redirect instead.
        if request.headers.get("hx-request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": "/login"})

        # A limited account is told why it was turned away and gets no ?next=:
        # sending it back here after login would only bounce it again.
        if limited:
            return RedirectResponse(url=f"/login?error={APP_ONLY_ERROR}", status_code=303)

        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(url=f"/login?next={target}", status_code=303)
