"""User administration, reachable only by the virtual admin account."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.abs import sessions
from app.auth import ADMIN_USERNAME, require_admin
from app.db import get_db
from app.models import User
from app.passwords import hash_password
from app.services.users import delete_user as delete_user_service
from app.services.users import review_orphans
from app.templating import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _users_page(
    request: Request,
    db: Session,
    error: str | None = None,
    status_code: int = 200,
    report=None,
):
    users = db.scalars(select(User).order_by(User.username)).all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"users": users, "error": error, "report": report},
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


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/users/{user_id}/enable")
def enable_user(user_id: int, db: Session = Depends(get_db)):
    _get_user(db, user_id).enabled = True
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/disable")
def disable_user(user_id: int, db: Session = Depends(get_db)):
    # Takes effect immediately: sessions and ABS tokens re-check the DB.
    _get_user(db, user_id).enabled = False
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/password")
def change_password(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    password: str = Form(""),
):
    user = _get_user(db, user_id)
    if not password:
        return _users_page(request, db, "Password must not be empty.", 422)
    user.password_hash = hash_password(password)
    db.commit()
    # A reset is how a compromised account gets taken back, so the old password's
    # logins have to die with it: without this an ABS client signed in before the
    # reset keeps working for its refresh token's full 30 days. Browser sessions
    # are signed cookies with no server-side record, so they are NOT covered —
    # disable the account instead if you need to lock someone out of the UI.
    sessions.revoke_all(db, user)
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/token")
def change_token(
    user_id: int,
    db: Session = Depends(get_db),
    hardcover_token: str = Form(""),
):
    user = _get_user(db, user_id)
    user.hardcover_token = hardcover_token.strip()
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/users/{user_id}/delete", response_class=HTMLResponse)
def delete_user_review(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Review page: books only this user has, with a per-book choice of
    delete-from-disk or leave-in-place before the account is removed."""
    user = _get_user(db, user_id)
    review = review_orphans(db, user)
    return templates.TemplateResponse(
        request,
        "admin_delete_user.html",
        {"user": user, "review": review},
    )


@router.post("/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    form = await request.form()
    delete_disk_ids = {
        int(name.removeprefix("disk_"))
        for name, value in form.multi_items()
        if name.startswith("disk_") and value == "delete"
    }
    # file IO can be slow (large audiobooks): keep it off the event loop
    report = await run_in_threadpool(delete_user_service, db, user, delete_disk_ids)
    return _users_page(request, db, report=report)
