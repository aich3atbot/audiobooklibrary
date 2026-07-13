"""JWTs for the Audiobookshelf-compatible API.

Mirrors ABS TokenManager: HS256, payload {userId, username, type, exp};
access tokens 1h, refresh 30d, legacy tokens without exp/type accepted
forever. Signed with the same persisted secret as the UI session cookie.
userId is the User row's stable uuid."""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.auth import resolve_session_secret
from app.models import User

ACCESS_TOKEN_EXPIRY = timedelta(hours=1)
REFRESH_TOKEN_EXPIRY = timedelta(days=30)


def _make(user: User, token_type: str | None, expiry: timedelta | None) -> str:
    payload: dict[str, Any] = {
        "userId": user.uuid,
        "username": user.username,
    }
    if token_type:
        payload["type"] = token_type
    if expiry:
        payload["exp"] = datetime.now(timezone.utc) + expiry
    return jwt.encode(payload, resolve_session_secret(), algorithm="HS256")


def create_access_token(user: User) -> str:
    return _make(user, "access", ACCESS_TOKEN_EXPIRY)


def create_refresh_token(user: User) -> str:
    return _make(user, "refresh", REFRESH_TOKEN_EXPIRY)


def create_legacy_token(user: User) -> str:
    # ABS "old" tokens: no type, no expiry — kept for client compatibility.
    return _make(user, None, None)


def verify_token(token: str) -> dict[str, Any] | None:
    """Return the payload if valid (signature + expiry), else None."""
    try:
        return jwt.decode(token, resolve_session_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
