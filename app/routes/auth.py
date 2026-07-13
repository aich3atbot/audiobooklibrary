from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import SESSION_USER_KEY, check_credentials, safe_next
from app.config import get_settings
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not get_settings().auth_enabled or request.session.get(SESSION_USER_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": safe_next(next), "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if not get_settings().auth_enabled:
        return RedirectResponse(url="/", status_code=303)
    if not check_credentials(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": safe_next(next), "error": "Invalid username or password."},
            status_code=401,
        )
    request.session[SESSION_USER_KEY] = get_settings().auth_username
    return RedirectResponse(url=safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
