import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Book
from app.services.downloads import grab_release, search_releases
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_book(db: Session, book_id: int) -> Book:
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


@router.get("/books/{book_id}/releases", response_class=HTMLResponse)
def list_releases(book_id: int, request: Request, db: Session = Depends(get_db)):
    book = _get_book(db, book_id)
    releases = []
    error = None
    try:
        releases = search_releases(book)
    except Exception:
        logger.exception("Indexer search failed for %s", book.title)
        error = "Search failed — check the indexer connection and try again."
    return templates.TemplateResponse(
        request,
        "_releases.html",
        {"book": book, "releases": releases, "error": error},
    )


@router.post("/books/{book_id}/grab", response_class=HTMLResponse)
def grab(
    book_id: int,
    request: Request,
    guid: str = Form(...),
    indexer: str = Form(...),
    title: str = Form(...),
    size: int | None = Form(None),
    db: Session = Depends(get_db),
):
    book = _get_book(db, book_id)
    try:
        grab_release(db, book, guid, indexer, title, size)
    except Exception:
        logger.exception("Grab failed for %s", book.title)
        return templates.TemplateResponse(
            request,
            "_releases.html",
            {"book": book, "releases": [], "error": "Grab failed — see the app log."},
        )
    # Close the modal and swap the book card out-of-band so its badge updates.
    return templates.TemplateResponse(request, "_grab_done.html", {"book": book})
