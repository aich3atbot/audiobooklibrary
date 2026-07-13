from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.abs.routes import abs_login
from app.auth import (
    SESSION_IS_ADMIN,
    SESSION_USER_ID,
    check_admin_credentials,
    safe_next,
)
from app.db import get_db
from app.models import User
from app.passwords import verify_password
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.session.get(SESSION_IS_ADMIN):
        return RedirectResponse(url="/admin/users", status_code=303)
    if request.session.get(SESSION_USER_ID) is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": safe_next(next), "error": None}
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

    if check_admin_credentials(username, password):
        request.session.clear()
        request.session[SESSION_IS_ADMIN] = True
        return RedirectResponse(url="/admin/users", status_code=303)

    user = db.scalar(select(User).where(User.username == username))
    if (
        user is not None
        and user.enabled
        # threadpool: scrypt takes tens of ms, don't block the event loop
        and await run_in_threadpool(verify_password, password, user.password_hash)
    ):
        request.session.clear()
        request.session[SESSION_USER_ID] = user.id
        return RedirectResponse(url=next_url, status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_url, "error": "Invalid username or password."},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
