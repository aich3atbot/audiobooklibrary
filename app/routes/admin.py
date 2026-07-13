"""User administration, reachable only by the virtual admin account."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import ADMIN_USERNAME, require_admin
from app.db import get_db
from app.models import User
from app.passwords import hash_password
from app.templating import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _users_page(request: Request, db: Session, error: str | None = None, status_code: int = 200):
    users = db.scalars(select(User).order_by(User.username)).all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"users": users, "error": error},
        status_code=status_code,
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    return _users_page(request, db)


@router.post("/users")
def create_user(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(""),
    password: str = Form(""),
    hardcover_token: str = Form(""),
):
    username = username.strip()
    if not username or not password:
        return _users_page(request, db, "Username and password are required.", 422)
    if username.lower() == ADMIN_USERNAME:
        return _users_page(request, db, '"admin" is reserved.', 422)

    user = User(
        username=username,
        password_hash=hash_password(password),
        hardcover_token=hardcover_token.strip(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _users_page(request, db, f'User "{username}" already exists.', 422)
    return RedirectResponse(url="/admin/users", status_code=303)
