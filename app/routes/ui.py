from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import (
    DISPLAY_DOWNLOADING,
    Author,
    Book,
    DownloadState,
    ReadState,
    Series,
    User,
    UserBook,
)
from app.services.sync import run_sync_for_user, update_read_state
from app.templating import templates

router = APIRouter()


SORTS = {
    "title": (Book.title,),
    "author": (Author.name, Book.series_index, Book.title),
    "recent": (Book.updated_at.desc(),),
}

# The three UI download statuses, mapped to the pipeline states they cover.
DL_FILTERS = {
    "not_present": (DownloadState.NONE, DownloadState.FAILED),
    "downloading": tuple(DISPLAY_DOWNLOADING),
    "available": (DownloadState.IMPORTED,),
}


def _enum_or_none(enum_cls, value):
    try:
        return enum_cls(value) if value else None
    except ValueError:
        return None


@router.get("/", response_class=HTMLResponse)
def library(
    request: Request,
    q: str = "",
    read: str = "",
    dl: str = "",
    sort: str = "title",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = (
        select(Book, UserBook)
        .join(UserBook, (UserBook.book_id == Book.id) & (UserBook.user_id == user.id))
        .join(Book.author)
        .outerjoin(Book.series)
        .options(joinedload(Book.author), joinedload(Book.series))
    )
    q = q.strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Book.title.ilike(like), Author.name.ilike(like), Series.name.ilike(like))
        )
    read_state = _enum_or_none(ReadState, read)
    if read_state:
        stmt = stmt.where(UserBook.read_state == read_state)
    dl = dl if dl in DL_FILTERS else ""
    if dl:
        stmt = stmt.where(Book.download_state.in_(DL_FILTERS[dl]))
    sort = sort if sort in SORTS else "title"
    stmt = stmt.order_by(*SORTS[sort])

    rows = db.execute(stmt).all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            "q": q,
            "read": read_state.value if read_state else "",
            "dl": dl,
            "sort": sort,
            "read_states": [s.value for s in ReadState],
            "dl_states": list(DL_FILTERS),
            "last_sync": user.last_sync_at,
            "last_sync_result": user.last_sync_result,
        },
    )


@router.post("/books/{book_id}/read-state", response_class=HTMLResponse)
def set_read_state(
    book_id: int,
    request: Request,
    state: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        new_state = ReadState(state)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid read state: {state}")
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    user_book = update_read_state(db, user, book, new_state)
    return templates.TemplateResponse(request, "_card.html", {"book": book, "ub": user_book})


@router.post("/sync")
def sync_now(user: User = Depends(get_current_user)):
    # Sync endpoint runs in the threadpool (def, not async def), so the
    # blocking HTTP calls to Hardcover don't stall the event loop.
    run_sync_for_user(user.id)
    return RedirectResponse(url="/", status_code=303)
