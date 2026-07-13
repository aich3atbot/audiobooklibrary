from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Book
from app.services.sync import LAST_SYNC_KEY, LAST_SYNC_RESULT_KEY, get_state, run_sync_once

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


@router.post("/sync")
def sync_now():
    # Sync endpoint runs in the threadpool (def, not async def), so the
    # blocking HTTP calls to Hardcover don't stall the event loop.
    run_sync_once()
    return RedirectResponse(url="/", status_code=303)
