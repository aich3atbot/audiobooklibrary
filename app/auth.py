"""Single-user session authentication.

Enforced only when AUTH_USERNAME and AUTH_PASSWORD are both set; without
them the app is open (trusted-LAN mode, the pre-auth behavior). The login
session lives in a signed cookie (Starlette SessionMiddleware)."""

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import get_settings

SESSION_USER_KEY = "user"
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


def is_logged_in(request: Request) -> bool:
    settings = get_settings()
    return request.session.get(SESSION_USER_KEY) == settings.auth_username


def check_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    # compare_digest on both fields to avoid timing side channels
    user_ok = secrets.compare_digest(username.encode(), settings.auth_username.encode())
    pass_ok = secrets.compare_digest(password.encode(), settings.auth_password.encode())
    return user_ok and pass_ok


def safe_next(next_url: str) -> str:
    """Only allow same-site relative redirect targets."""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


class RequireAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        path = request.url.path
        if (
            not settings.auth_enabled
            or path in OPEN_PATHS
            or path.startswith(OPEN_PREFIXES)
            or is_logged_in(request)
        ):
            return await call_next(request)

        # HTMX fragment requests can't render a redirect target themselves;
        # tell htmx to do a full-page redirect instead.
        if request.headers.get("hx-request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": "/login"})

        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(url=f"/login?next={target}", status_code=303)
