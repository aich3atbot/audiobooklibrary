from typing import Any

from fastapi import HTTPException, Request

from app.abs.tokens import verify_token
from app.config import get_settings


def require_abs_user(request: Request) -> dict[str, Any]:
    """Authenticate an ABS API request: Bearer header or ?token= query param.
    Refresh tokens are not valid here. When UI auth is disabled the API is
    open too (trusted-LAN mode), matching the rest of the app."""
    settings = get_settings()
    if not settings.auth_enabled:
        return {"userId": "open", "username": settings.auth_username or "user"}

    token = None
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = request.query_params.get("token")

    payload = verify_token(token) if token else None
    if payload is None or payload.get("type") == "refresh":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return payload
