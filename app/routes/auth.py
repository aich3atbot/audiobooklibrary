from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.abs.routes import abs_login
from app.auth import SESSION_USER_KEY, check_credentials, safe_next
from app.config import get_settings
from app.db import get_db
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not get_settings().auth_enabled or request.session.get(SESSION_USER_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": safe_next(next), "error": None}
    )


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    # ABS clients POST JSON {username, password}; the web UI posts a form.
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        return abs_login(request, body.get("username", ""), body.get("password", ""), db)

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    next_url = safe_next(form.get("next", "/"))

    if not get_settings().auth_enabled:
        return RedirectResponse(url="/", status_code=303)
    if not check_credentials(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next_url, "error": "Invalid username or password."},
            status_code=401,
        )
    request.session[SESSION_USER_KEY] = get_settings().auth_username
    return RedirectResponse(url=next_url, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
