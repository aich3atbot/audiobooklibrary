from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.abs.tokens import verify_token
from app.db import get_db
from app.models import User


def require_abs_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Authenticate an ABS API request: Bearer header or ?token= query param.
    Refresh tokens are not valid here. The token must resolve to an enabled
    account — disabling a user invalidates their tokens immediately, and the
    admin account is never one (it has no library; no token is ever minted for
    it, and this makes sure a hand-made one would not work either)."""
    token = None
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = request.query_params.get("token")

    payload = verify_token(token) if token else None
    if payload is None or payload.get("type") == "refresh":
        raise HTTPException(status_code=401, detail="Unauthorized")

    user = db.scalar(select(User).where(User.uuid == payload.get("userId", "")))
    if user is None or user.is_admin or not user.enabled:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user
