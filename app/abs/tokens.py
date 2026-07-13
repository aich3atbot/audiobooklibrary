"""JWTs for the Audiobookshelf-compatible API.

Mirrors ABS TokenManager: HS256, payload {userId, username, type, exp};
access tokens 1h, refresh 30d, legacy tokens without exp/type accepted
forever. Signed with the same persisted secret as the UI session cookie."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.auth import resolve_session_secret
from app.config import get_settings

ACCESS_TOKEN_EXPIRY = timedelta(hours=1)
REFRESH_TOKEN_EXPIRY = timedelta(days=30)

# Deterministic ABS user id for our single user.
USER_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def user_id() -> str:
    return str(uuid.uuid5(USER_NAMESPACE, f"audiobooklibrary-{get_settings().auth_username}"))


def _make(token_type: str | None, expiry: timedelta | None) -> str:
    payload: dict[str, Any] = {
        "userId": user_id(),
        "username": get_settings().auth_username,
    }
    if token_type:
        payload["type"] = token_type
    if expiry:
        payload["exp"] = datetime.now(timezone.utc) + expiry
    return jwt.encode(payload, resolve_session_secret(), algorithm="HS256")


def create_access_token() -> str:
    return _make("access", ACCESS_TOKEN_EXPIRY)


def create_refresh_token() -> str:
    return _make("refresh", REFRESH_TOKEN_EXPIRY)


def create_legacy_token() -> str:
    # ABS "old" tokens: no type, no expiry — kept for client compatibility.
    return _make(None, None)


def verify_token(token: str) -> dict[str, Any] | None:
    """Return the payload if valid (signature + expiry), else None."""
    try:
        return jwt.decode(token, resolve_session_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
