import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.db import get_db
from app.models import Book, ReadState
from app.services.sync import add_book
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

ADDABLE_STATES = (ReadState.WANT_TO_READ, ReadState.READING, ReadState.READ)


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    q = q.strip()
    results = []
    error = None
    in_library: dict[int, Book] = {}
    if q:
        try:
            with HardcoverClient(get_settings().hardcover_token) as client:
                results = client.search_books(q)
        except Exception:
            logger.exception("Hardcover search failed")
            error = "Hardcover search failed — check the connection and try again."
        if results:
            ids = [r["hardcover_id"] for r in results]
            in_library = {
                b.hardcover_id: b
                for b in db.scalars(select(Book).where(Book.hardcover_id.in_(ids)))
            }
    return templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results, "in_library": in_library, "error": error},
    )


@router.post("/search/add", response_class=HTMLResponse)
def search_add(
    request: Request,
    hardcover_id: int = Form(...),
    state: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        read_state = ReadState(state)
    except ValueError:
        read_state = None
    if read_state not in ADDABLE_STATES:
        raise HTTPException(status_code=422, detail=f"invalid state for add: {state}")
    book = add_book(db, hardcover_id, read_state)
    # Swap the search result for a normal library card so the read-state
    # selector is immediately live.
    return templates.TemplateResponse(request, "_card.html", {"book": book})
