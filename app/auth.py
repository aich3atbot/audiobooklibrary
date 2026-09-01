"""Multi-user session authentication.

Accounts are mandatory: every UI route requires a logged-in user (or the
admin, who only sees user administration). The signed session cookie carries
nothing but an opaque token; the session itself is an `auth_session` row
(`app/abs/sessions.py`), which is what makes a browser login revocable — from
an app's device list, from a password change, or by signing out. The user is
re-read on every request too, so disabling an account locks it out
immediately.

*Limited* accounts (`UserRole.LIMITED`) are ABS-only: they authenticate over
the API surface (`/api/`, `/auth/`) but never here. The per-request re-check
is what enforces that — a user demoted mid-session loses the UI on their very
next request.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.abs import sessions
from app.db import get_db, get_sessionmaker
from app.models import User

# The cookie holds only this: the opaque token naming the session row.
SESSION_TOKEN = "sid"
# Nothing here knows the administrator's *name*: authentication is by password
# against the row, and which UI you get is decided by `user.is_admin`. The name
# only exists at startup, to create the row (app/services/users.py).
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


def safe_next(next_url: str) -> str:
    """Only allow same-site relative redirect targets."""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """The logged-in regular user. The middleware resolved the session and
    left the id on the request; re-queried here so the row is attached to the
    route's own database session."""
    user_id = getattr(request.state, "user_id", None)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None or not user.enabled:
        raise HTTPException(status_code=401, detail="Not logged in")
    # The middleware already turned these away; this is the backstop for any
    # UI route reached without passing through it.
    if user.is_limited:
        raise HTTPException(status_code=403, detail=APP_ONLY_MESSAGE)
    if user.is_admin:
        raise HTTPException(status_code=403, detail="The admin account has no library")
    return user


def require_admin(request: Request) -> None:
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")


class RequireAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.user_id = None
        request.state.username = None
        request.state.is_admin = False
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)

        limited = False
        token = request.session.get(SESSION_TOKEN)
        if token:
            with get_sessionmaker()() as db:
                user = sessions.resolve_ui(db, token)
            if user is not None and user.enabled and not user.is_limited:
                request.state.user_id = user.id
                request.state.username = user.username
                request.state.is_admin = user.is_admin
                if user.is_admin:
                    # The admin account only administers users.
                    if not path.startswith("/admin") and path != "/logout":
                        return RedirectResponse(url="/admin/users", status_code=303)
                elif path.startswith("/admin"):
                    return RedirectResponse(url="/", status_code=303)
                return await call_next(request)
            # Revoked, deleted, disabled or demoted mid-session: drop the
            # stale cookie so the next request doesn't repeat the lookup.
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
