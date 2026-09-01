from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.abs import sessions
from app.abs.routes import REFRESH_COOKIE, abs_login, refresh_token_from
from app.auth import (
    APP_ONLY_ERROR,
    APP_ONLY_MESSAGE,
    SESSION_TOKEN,
    safe_next,
)
from app.db import get_db
from app.models import User
from app.passwords import verify_password
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = "/",
    error: str = "",
    db: Session = Depends(get_db),
):
    token = request.session.get(SESSION_TOKEN)
    # /login is open, so the middleware resolved nothing: check the session
    # here or an already-signed-in visitor gets the form back.
    user = sessions.resolve_ui(db, token) if token else None
    if user is not None and user.enabled and not user.is_limited:
        return RedirectResponse(
            url="/admin/users" if user.is_admin else "/", status_code=303
        )
    # The middleware turns limited accounts away with ?error=app_only; the key
    # maps to a fixed message here so nothing arbitrary is rendered.
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": safe_next(next),
            "error": APP_ONLY_MESSAGE if error == APP_ONLY_ERROR else None,
        },
    )


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    # ABS clients POST JSON {username, password}; the web UI posts a form.
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        return await run_in_threadpool(
            abs_login, request, body.get("username", ""), body.get("password", ""), db
        )

    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = safe_next(form.get("next", "/"))

    # The admin is an ordinary row (username "admin", UserRole.ADMIN), so it
    # authenticates through this same path and gets a revocable session.
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is not None
        and user.enabled
        # threadpool: scrypt takes tens of ms, don't block the event loop
        and await run_in_threadpool(verify_password, password, user.password_hash)
    ):
        # Limited accounts are ABS-only. Say so rather than "invalid password":
        # the credentials are right, this door just isn't theirs. The JSON
        # branch above (abs_login) is the one they use.
        if user.is_limited:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"next": next_url, "error": APP_ONLY_MESSAGE},
                status_code=403,
            )
        request.session.clear()
        request.session[SESSION_TOKEN] = sessions.create_ui(db, user, request)
        # The admin has no library to be sent back to.
        target = "/admin/users" if user.is_admin else next_url
        return RedirectResponse(url=target, status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_url, "error": "Invalid username or password."},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request, allDevices: str = "", db: Session = Depends(get_db)):  # noqa: N803
    """Sign out. Two callers share this route: the UI form (cookie session,
    wants the login page back) and ABS clients (refresh token in a header,
    want JSON). Deleting the session row is what makes it stick — both
    credentials are self-contained and would otherwise keep working for 30
    days.

    `?allDevices=1` drops every session the user has, not just this one."""
    ui_token = request.session.get(SESSION_TOKEN)
    request.session.clear()
    revoked = sessions.revoke_ui(db, ui_token) if ui_token else None

    refresh_token = refresh_token_from(request)
    if refresh_token:
        revoked = sessions.revoke(db, refresh_token) or revoked
    if revoked is not None and allDevices == "1":
        user = db.get(User, revoked.user_id)
        if user is not None:
            sessions.revoke_all(db, user)

    wants_json = (
        request.headers.get("x-refresh-token") is not None
        or request.headers.get("authorization", "").lower().startswith("bearer ")
    )
    response = (
        JSONResponse({"success": True})
        if wants_json
        else RedirectResponse(url="/login", status_code=303)
    )
    response.delete_cookie(REFRESH_COOKIE)
    return response
