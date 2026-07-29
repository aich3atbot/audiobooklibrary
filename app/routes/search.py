import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.clients.hardcover import HardcoverClient
from app.db import get_db
from app.models import Book, ReadState, Series, User, UserBook
from app.routes.downloads import list_releases
from app.services.sync import add_book, get_user_book
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

ADDABLE_STATES = (ReadState.WANT_TO_READ, ReadState.READING, ReadState.READ)

NO_TOKEN_ERROR = "No Hardcover token set for your account — ask the admin to add one."


def _local_state(
    db: Session, user: User, hardcover_ids: list[int]
) -> tuple[dict[int, Book], dict[int, UserBook]]:
    """Local Book rows and the user's own shelf entries for the given
    Hardcover book ids, each keyed by hardcover id."""
    if not hardcover_ids:
        return {}, {}
    # Books anyone brought into the shared store, keyed by hardcover id
    known = {
        b.hardcover_id: b
        for b in db.scalars(select(Book).where(Book.hardcover_id.in_(hardcover_ids)))
    }
    mine = {}
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
    return known, mine


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
        error = NO_TOKEN_ERROR
    elif q:
        try:
            with HardcoverClient(user.hardcover_token) as client:
                results = client.search_books(q)
        except Exception:
            logger.exception("Hardcover search failed")
            error = "Hardcover search failed — check the connection and try again."
        known, mine = _local_state(db, user, [r["hardcover_id"] for r in results])
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


def _series_context(db: Session, user: User, series_id: int) -> dict:
    """The full series as Hardcover knows it, merged with local state."""
    name = None
    slug = None
    books = []
    error = None
    known: dict[int, Book] = {}
    mine: dict[int, UserBook] = {}
    if not user.hardcover_token:
        error = NO_TOKEN_ERROR
    else:
        try:
            with HardcoverClient(user.hardcover_token) as client:
                name, slug, books = client.fetch_series(series_id)
        except Exception:
            logger.exception("Hardcover series lookup failed")
            error = "Hardcover series lookup failed — check the connection and try again."
        known, mine = _local_state(db, user, [b["hardcover_id"] for b in books])
    if name is None or not slug:
        # the stored row carries us through a failed lookup
        local = db.scalar(select(Series).where(Series.hardcover_id == series_id))
        if name is None:
            name = local.name if local else "Series"
        slug = slug or (local.hardcover_slug if local else None)
    return {
        "series_name": name,
        "series_slug": slug,
        "series_id": series_id,
        "results": books,
        "known": known,
        "mine": mine,
        "error": error,
        "addable": sum(1 for b in books if b["hardcover_id"] not in mine),
    }


@router.get("/series/{series_id}", response_class=HTMLResponse)
def series_page(
    series_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Shelved books render as live library cards, the rest as addable search
    results."""
    return templates.TemplateResponse(
        request, "series.html", _series_context(db, user, series_id)
    )


@router.post("/series/{series_id}/add-all", response_class=HTMLResponse)
def series_add_all(
    series_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Shelve every series book the user hasn't shelved yet as want-to-read.
    Books already on their shelf (any state) are left untouched. Per-book
    failures don't abort the rest; they surface in the error banner."""
    context = _series_context(db, user, series_id)
    added = failed = 0
    for entry in context["results"]:
        if entry["hardcover_id"] in context["mine"]:
            continue
        try:
            add_book(db, user, entry["hardcover_id"], ReadState.WANT_TO_READ)
            added += 1
        except Exception:
            logger.exception("Add-all: adding hardcover id %s failed", entry["hardcover_id"])
            failed += 1
    known, mine = _local_state(db, user, [b["hardcover_id"] for b in context["results"]])
    context.update(
        known=known,
        mine=mine,
        addable=sum(1 for b in context["results"] if b["hardcover_id"] not in mine),
    )
    if failed:
        context["error"] = f"Added {added} of {added + failed} books; the rest failed — try again."
    return templates.TemplateResponse(request, "_series_content.html", context)


@router.post("/series/{hardcover_id}/download", response_class=HTMLResponse)
def series_download(
    hardcover_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Download a series book that may not be on the user's shelf yet:
    downloading implies wanting it, so shelve it as want-to-read first, then
    open the normal release picker."""
    book = db.scalar(select(Book).where(Book.hardcover_id == hardcover_id))
    if book is None or get_user_book(db, user, book) is None:
        try:
            book = add_book(db, user, hardcover_id, ReadState.WANT_TO_READ)
        except Exception:
            logger.exception("Add before download failed for hardcover id %s", hardcover_id)
            return templates.TemplateResponse(
                request,
                "_releases.html",
                {
                    "book": {"id": 0, "title": "?"},
                    "releases": [],
                    "error": "Adding the book to your Hardcover shelf failed — try again.",
                },
            )
    return list_releases(book.id, request, db=db)
