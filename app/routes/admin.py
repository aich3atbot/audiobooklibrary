"""User administration, reachable only by the admin account.

The admin is a `user` row itself now (so its own sessions can be revoked), but
it is not administrable from here: it is filtered out of the list *by role*
and `_get_user` refuses it, so no verb can disable, delete, demote or
re-password it by id. Its password comes from ADMIN_PASSWORD at startup and
nowhere else.

No username is treated as special: a new account called "admin" is refused
only because the unique index already holds it, and a lookalike name grants
nothing, since every check here is on `UserRole.ADMIN`."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.abs import sessions
from app.auth import require_admin
from app.db import get_db
from app.models import User, UserRole
from app.passwords import hash_password
from app.services.users import delete_user as delete_user_service
from app.services.users import review_orphans
from app.templating import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

LIMITED_NO_TOKEN = "Limited accounts have no Hardcover access."


def _parse_role(value: str) -> UserRole:
    """Anything unrecognised is a full account — the form's default."""
    return UserRole.LIMITED if value == UserRole.LIMITED.value else UserRole.FULL


def _users_page(
    request: Request,
    db: Session,
    error: str | None = None,
    status_code: int = 200,
    report=None,
):
    users = db.scalars(
        select(User).where(User.role != UserRole.ADMIN).order_by(User.username)
    ).all()
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
    role: str = Form(UserRole.FULL.value),
):
    username = username.strip()
    if not username or not password:
        return _users_page(request, db, "Username and password are required.", 422)

    # No name is special here: the administrator is a row like any other, and
    # the unique index on username is what stops a second one taking its name.
    parsed_role = _parse_role(role)
    user = User(
        username=username,
        password_hash=hash_password(password),
        # "limited means no Hardcover" is an invariant, not a convention: a
        # token posted alongside the limited role is dropped, not stored.
        hardcover_token="" if parsed_role == UserRole.LIMITED else hardcover_token.strip(),
        role=parsed_role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _users_page(request, db, f'User "{username}" already exists.', 422)
    return RedirectResponse(url="/admin/users", status_code=303)


def _get_user(db: Session, user_id: int) -> User:
    """The account a management verb acts on. The admin's own row is not one:
    it is invisible to this page, and every verb here would break it (there is
    no other administrator to enable it again, and its password would be reset
    from the environment at the next restart anyway)."""
    user = db.get(User, user_id)
    if user is None or user.is_admin:
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
    # are rows too, so they go the same way — whoever knew the old password is
    # out of the web UI on their next request.
    sessions.revoke_all(db, user)
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/token")
def change_token(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    hardcover_token: str = Form(""),
):
    user = _get_user(db, user_id)
    if user.is_limited:
        return _users_page(request, db, LIMITED_NO_TOKEN, 422)
    user.hardcover_token = hardcover_token.strip()
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/role")
def change_role(
    user_id: int,
    db: Session = Depends(get_db),
    role: str = Form(UserRole.FULL.value),
):
    """Switch an account between full and limited.

    Demoting clears the Hardcover token — the account loses its Hardcover
    identity, and a stored credential nothing reads is just a liability. Their
    existing user_book rows are left alone so promoting them back restores
    their library. ABS sessions are deliberately NOT revoked: app access is
    precisely what a limited account keeps, and the UI lockout is immediate
    anyway (the middleware re-checks the row per request)."""
    user = _get_user(db, user_id)
    user.role = _parse_role(role)
    if user.is_limited:
        user.hardcover_token = ""
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
