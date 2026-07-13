from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Book, ReadState
from app.services.sync import (
    LAST_SYNC_KEY,
    LAST_SYNC_RESULT_KEY,
    get_state,
    run_sync_once,
    update_read_state,
)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/", response_class=HTMLResponse)
def library(request: Request, db: Session = Depends(get_db)):
    books = (
        db.execute(
            select(Book)
            .options(joinedload(Book.author), joinedload(Book.series))
            .order_by(Book.title)
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "books": books,
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
