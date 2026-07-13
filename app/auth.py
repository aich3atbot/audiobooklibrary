"""Multi-user session authentication.

Accounts are mandatory: every UI route requires a logged-in user (or the
virtual admin, who only sees user administration). The login session lives
in a signed cookie (Starlette SessionMiddleware); regular users are
re-checked against the database on every request so disabling an account
locks it out immediately.
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
OPEN_PATHS = {"/login", "/healthz", "/status", "/ping", "/healthcheck"}
# /api/ and /auth/ are the ABS surface: bearer-token auth enforced per-route
# (app/abs/deps.py), not by this cookie-redirect middleware.
OPEN_PREFIXES = ("/static/", "/api/", "/auth/")


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

        user_id = request.session.get(SESSION_USER_ID)
        if user_id is not None:
            with get_sessionmaker()() as db:
                user = db.get(User, user_id)
            if user is not None and user.enabled:
                request.state.user_id = user.id
                request.state.username = user.username
                if path.startswith("/admin"):
                    return RedirectResponse(url="/", status_code=303)
                return await call_next(request)
            # Deleted or disabled mid-session: drop the stale session.
            request.session.clear()

        # HTMX fragment requests can't render a redirect target themselves;
        # tell htmx to do a full-page redirect instead.
        if request.headers.get("hx-request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": "/login"})

        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(url=f"/login?next={target}", status_code=303)
