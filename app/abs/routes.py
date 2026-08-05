"""Audiobookshelf-compatible API: discovery + auth endpoints.
Contract: docs/abs-api-contract.md."""

import logging
import uuid
from math import ceil

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.abs import payloads, sessions, tokens
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
    sessions.create(db, user, refresh_token, request)
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


def refresh_token_from(request: Request) -> str | None:
    """Mobile clients send the refresh token as a header, browsers as a cookie."""
    return request.headers.get("x-refresh-token") or request.cookies.get(REFRESH_COOKIE)


@router.post("/auth/refresh")
def refresh(request: Request, db: Session = Depends(get_db)):
    header_token = request.headers.get("x-refresh-token")
    return_refresh = header_token is not None  # header in, header out
    refresh_token = header_token or request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        return JSONResponse({"error": "No refresh token provided"}, status_code=401)

    payload = tokens.verify_token(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        return JSONResponse({"error": "Invalid refresh token"}, status_code=401)
    user = db.scalar(select(User).where(User.uuid == payload.get("userId", "")))
    if user is None or not user.enabled:
        return JSONResponse({"error": "Invalid refresh token"}, status_code=401)

    # A valid signature is not enough: the session must still exist, which is
    # what makes logout (and revoking a device) actually revoke.
    session, is_grace = sessions.resolve(db, refresh_token)
    if session is None or session.user_id != user.id:
        return JSONResponse({"error": "Invalid refresh token"}, status_code=401)

    new_access = tokens.create_access_token(user)
    if is_grace:
        # A concurrent refresh already rotated this session, so rotating again
        # would invalidate the token that won. Upstream answers with the
        # winning refresh token; we only keep its hash, so we answer with a
        # fresh access token and no refresh token — clients keep the one they
        # hold (it stays valid for the rest of the window) and pick up the
        # winner's from their own persisted storage.
        sessions.touch(db, session)
        return JSONResponse(payloads.login_payload(db, user, new_access, None))

    new_refresh = tokens.create_refresh_token(user)
    sessions.rotate(db, session, new_refresh)
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


@router.get("/api/me/sessions")
def my_sessions(
    request: Request,
    page: int = 0,
    itemsPerPage: int = 10,  # noqa: N803 — the client's query param name
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    """The user's signed-in devices. `current` is resolved from the refresh
    token on the request, so a client can tell which row is itself."""
    page = max(0, page)
    per_page = max(1, itemsPerPage)
    rows = sessions.active_for(db, user)
    total = len(rows)
    presented = refresh_token_from(request)
    current = sessions.find(db, presented) if presented else None
    return {
        "total": total,
        "numPages": ceil(total / per_page),
        "page": page,
        "itemsPerPage": per_page,
        "sessions": [
            sessions.session_json(s, current is not None and s.id == current.id)
            for s in rows[page * per_page : page * per_page + per_page]
        ],
    }


@router.delete("/api/me/sessions/{session_id}")
def delete_my_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    """Sign one device out. Ids are uuids; upstream 400s on anything else."""
    try:
        uuid.UUID(session_id)
    except ValueError:
        return Response(status_code=400)
    if not sessions.revoke_by_uuid(db, user, session_id):
        return Response(status_code=404)
    return Response(status_code=200)
