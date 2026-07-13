"""Audiobookshelf-compatible API: discovery + auth endpoints.
Contract: docs/abs-api-contract.md."""

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.abs import payloads, tokens
from app.abs.deps import require_abs_user
from app.auth import check_credentials
from app.config import get_settings
from app.db import get_db

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
    """JSON login for ABS clients (the UI form path lives in routes/auth.py)."""
    settings = get_settings()
    if settings.auth_enabled and not check_credentials(username, password):
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)

    return_tokens = request.headers.get("x-return-tokens") == "true"
    access_token = tokens.create_access_token()
    refresh_token = tokens.create_refresh_token()
    payload = payloads.login_payload(db, access_token, refresh_token if return_tokens else None)
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

    new_access = tokens.create_access_token()
    new_refresh = tokens.create_refresh_token()
    body = payloads.login_payload(db, new_access, new_refresh if return_refresh else None)
    response = JSONResponse(body)
    if not return_refresh:
        response.set_cookie(
            REFRESH_COOKIE, new_refresh, httponly=True, samesite="lax",
            max_age=int(tokens.REFRESH_TOKEN_EXPIRY.total_seconds()),
        )
    return response


@router.post("/api/authorize")
def authorize(request: Request, db: Session = Depends(get_db), user=Depends(require_abs_user)):
    access_token = tokens.create_access_token()
    return payloads.login_payload(db, access_token, None)


@router.get("/api/me")
def me(db: Session = Depends(get_db), user=Depends(require_abs_user)):
    return payloads.user_json(db)
