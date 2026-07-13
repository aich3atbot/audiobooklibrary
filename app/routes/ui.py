from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Author, Book, DownloadState, ReadState, Series
from app.services.sync import (
    LAST_SYNC_KEY,
    LAST_SYNC_RESULT_KEY,
    get_state,
    run_sync_once,
    update_read_state,
)
from app.templating import templates

router = APIRouter()


SORTS = {
    "title": (Book.title,),
    "author": (Author.name, Book.series_index, Book.title),
    "recent": (Book.updated_at.desc(),),
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
):
    stmt = (
        select(Book)
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
        stmt = stmt.where(Book.read_state == read_state)
    dl_state = _enum_or_none(DownloadState, dl)
    if dl_state:
        stmt = stmt.where(Book.download_state == dl_state)
    sort = sort if sort in SORTS else "title"
    stmt = stmt.order_by(*SORTS[sort])

    books = db.execute(stmt).scalars().all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "books": books,
            "q": q,
            "read": read_state.value if read_state else "",
            "dl": dl_state.value if dl_state else "",
            "sort": sort,
            "read_states": [s.value for s in ReadState],
            "dl_states": [s.value for s in DownloadState],
            "last_sync": get_state(db, LAST_SYNC_KEY),
            "last_sync_result": get_state(db, LAST_SYNC_RESULT_KEY),
        },
    )


@router.post("/books/{book_id}/read-state", response_class=HTMLResponse)
def set_read_state(
    book_id: int, request: Request, state: str = Form(...), db: Session = Depends(get_db)
):
    try:
        new_state = ReadState(state)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid read state: {state}")
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    book = update_read_state(db, book, new_state)
    return templates.TemplateResponse(request, "_card.html", {"book": book})


@router.post("/sync")
def sync_now():
    # Sync endpoint runs in the threadpool (def, not async def), so the
    # blocking HTTP calls to Hardcover don't stall the event loop.
    run_sync_once()
    return RedirectResponse(url="/", status_code=303)
