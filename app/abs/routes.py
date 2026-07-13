"""Audiobookshelf-compatible API: discovery + auth endpoints.
Contract: docs/abs-api-contract.md."""

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.abs import payloads, tokens
from app.abs.deps import require_abs_user
from app.auth import ADMIN_USERNAME
from app.db import get_db
from app.models import User
from app.passwords import verify_password

logger = logging.getLogger(__name__)

router = APIRouter()

REFRESH_COOKIE = "refresh_token"


@router.get("/status")
def status():
    return {
        "app": "audiobookshelf",
        "serverVersion": payloads.SERVER_VERSION,
        "isInit": True,
        "language": "en-us",
        "authMethods": ["local"],
        "authFormData": {},
    }


@router.get("/ping")
def ping():
    return {"success": True}


@router.get("/healthcheck")
def healthcheck():
    return Response(status_code=200)


def abs_login(request: Request, username: str, password: str, db: Session) -> JSONResponse:
    """JSON login for ABS clients (the UI form path lives in routes/auth.py).
    The admin account has no library and cannot use the ABS API."""
    user = None
    if username.strip() != ADMIN_USERNAME:
        user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.enabled or not verify_password(password, user.password_hash):
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)

    return_tokens = request.headers.get("x-return-tokens") == "true"
    access_token = tokens.create_access_token(user)
    refresh_token = tokens.create_refresh_token(user)
    payload = payloads.login_payload(
        db, user, access_token, refresh_token if return_tokens else None
    )
    response = JSONResponse(payload)
    if not return_tokens:
        response.set_cookie(
            REFRESH_COOKIE, refresh_token, httponly=True, samesite="lax",
            max_age=int(tokens.REFRESH_TOKEN_EXPIRY.total_seconds()),
        )
    return response


@router.post("/auth/refresh")
def refresh(request: Request, db: Session = Depends(get_db)):
    refresh_token = request.headers.get("x-refresh-token")
    return_refresh = refresh_token is not None
    if not refresh_token:
        refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        return JSONResponse({"error": "No refresh token provided"}, status_code=401)

    payload = tokens.verify_token(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        return JSONResponse({"error": "Invalid refresh token"}, status_code=401)
    user = db.scalar(select(User).where(User.uuid == payload.get("userId", "")))
    if user is None or not user.enabled:
        return JSONResponse({"error": "Invalid refresh token"}, status_code=401)

    new_access = tokens.create_access_token(user)
    new_refresh = tokens.create_refresh_token(user)
    body = payloads.login_payload(db, user, new_access, new_refresh if return_refresh else None)
    response = JSONResponse(body)
    if not return_refresh:
        response.set_cookie(
            REFRESH_COOKIE, new_refresh, httponly=True, samesite="lax",
            max_age=int(tokens.REFRESH_TOKEN_EXPIRY.total_seconds()),
        )
    return response


@router.post("/api/authorize")
def authorize(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    access_token = tokens.create_access_token(user)
    return payloads.login_payload(db, user, access_token, None)


@router.get("/api/me")
def me(db: Session = Depends(get_db), user: User = Depends(require_abs_user)):
    return payloads.user_json(db, user)
