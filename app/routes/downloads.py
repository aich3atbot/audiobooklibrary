import logging
from pathlib import Path

import mutagen
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Book, User, display_status
from app.services.downloads import grab_release, search_releases
from app.services.importer import AUDIO_EXTS, remove_library_files, replace_key
from app.services.sync import get_user_book, set_state
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_book(db: Session, book_id: int) -> Book:
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


def _grab_blocked(book: Book) -> str | None:
    """A book that is already downloading or available must not be grabbed
    again — the store is shared across users."""
    status = display_status(book.download_state)
    if status == "downloading":
        return "This book is already downloading."
    if status == "available":
        return "This book is already available in the library."
    return None


def _bitrate(path: Path) -> int | None:
    if path.suffix.lower() not in AUDIO_EXTS:
        return None
    try:
        parsed = mutagen.File(path)
        # a tag-less file is dict-like and falsy, so compare against None
        return getattr(parsed.info, "bitrate", None) if parsed is not None else None
    except Exception:
        return None


@router.get("/books/{book_id}/files", response_class=HTMLResponse)
def list_files(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Details of a downloaded book's files, with a replace-download entry."""
    book = _get_book(db, book_id)
    files = []
    error = None
    root = Path(book.library_path) if book.library_path else None
    if display_status(book.download_state) != "available" or root is None or not root.is_dir():
        error = "This book has no downloaded files."
    else:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files.append(
                    {
                        "rel_path": str(p.relative_to(root)),
                        "size": p.stat().st_size,
                        "bitrate": _bitrate(p),
                    }
                )
    return templates.TemplateResponse(
        request, "_files.html", {"book": book, "files": files, "error": error}
    )


@router.get("/books/{book_id}/releases", response_class=HTMLResponse)
def list_releases(
    book_id: int, request: Request, replace: bool = False, db: Session = Depends(get_db)
):
    book = _get_book(db, book_id)
    replacing = replace and display_status(book.download_state) == "available"
    blocked = None if replacing else _grab_blocked(book)
    if blocked:
        return templates.TemplateResponse(
            request, "_releases.html", {"book": book, "releases": [], "error": blocked}
        )
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
        {"book": book, "releases": releases, "error": error, "replace": replacing},
    )


@router.post("/books/{book_id}/grab", response_class=HTMLResponse)
def grab(
    book_id: int,
    request: Request,
    guid: str = Form(...),
    indexer: str = Form(...),
    title: str = Form(...),
    size: int | None = Form(None),
    replace: bool = Form(False),
    remove: str = Form("after_import"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    book = _get_book(db, book_id)
    replacing = replace and display_status(book.download_state) == "available"
    blocked = _grab_blocked(book)
    if blocked and not replacing:
        raise HTTPException(status_code=409, detail=blocked)
    try:
        release = grab_release(db, user, book, guid, indexer, title, size)
    except Exception:
        logger.exception("Grab failed for %s", book.title)
        return templates.TemplateResponse(
            request,
            "_releases.html",
            {"book": book, "releases": [], "error": "Grab failed — see the app log."},
        )
    if replacing:
        if remove == "immediately":
            try:
                remove_library_files(book)
            except Exception:
                # Fall back to removal at import time so the new download
                # doesn't die on the dest-exists guard.
                logger.exception("Could not remove library files for %s", book.title)
                set_state(db, replace_key(release), "1")
        else:
            set_state(db, replace_key(release), "1")
        db.commit()
    # Close the modal and swap the book card out-of-band so its badge updates.
    return templates.TemplateResponse(
        request,
        "_grab_done.html",
        {"book": book, "ub": get_user_book(db, user, book)},
    )
