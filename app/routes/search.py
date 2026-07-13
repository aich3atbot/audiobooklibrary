import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.clients.hardcover import HardcoverClient
from app.db import get_db
from app.models import Book, ReadState, User, UserBook
from app.services.sync import add_book, get_user_book
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

ADDABLE_STATES = (ReadState.WANT_TO_READ, ReadState.READING, ReadState.READ)


@router.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = q.strip()
    results = []
    error = None
    known: dict[int, Book] = {}
    mine: dict[int, UserBook] = {}
    if q and not user.hardcover_token:
        error = "No Hardcover token set for your account — ask the admin to add one."
    elif q:
        try:
            with HardcoverClient(user.hardcover_token) as client:
                results = client.search_books(q)
        except Exception:
            logger.exception("Hardcover search failed")
            error = "Hardcover search failed — check the connection and try again."
        if results:
            ids = [r["hardcover_id"] for r in results]
            # Books anyone brought into the shared store, keyed by hardcover id
            known = {
                b.hardcover_id: b
                for b in db.scalars(select(Book).where(Book.hardcover_id.in_(ids)))
            }
            if known:
                mine = {
                    ub.book.hardcover_id: ub
                    for ub in db.scalars(
                        select(UserBook)
                        .join(UserBook.book)
                        .where(
                            UserBook.user_id == user.id,
                            Book.hardcover_id.in_(list(known)),
                        )
                    )
                }
    return templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results, "known": known, "mine": mine, "error": error},
    )


@router.post("/search/add", response_class=HTMLResponse)
def search_add(
    request: Request,
    hardcover_id: int = Form(...),
    state: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        read_state = ReadState(state)
    except ValueError:
        read_state = None
    if read_state not in ADDABLE_STATES:
        raise HTTPException(status_code=422, detail=f"invalid state for add: {state}")
    book = add_book(db, user, hardcover_id, read_state)
    user_book = get_user_book(db, user, book)
    # Swap the search result for a normal library card so the read-state
    # selector is immediately live.
    return templates.TemplateResponse(request, "_card.html", {"book": book, "ub": user_book})
