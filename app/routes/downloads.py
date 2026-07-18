import logging
from pathlib import Path

import mutagen
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.models import Book, Edition, User, book_status
from app.services.downloads import grab_release, search_releases
from app.services.editions import relabel_edition
from app.services.importer import (
    AUDIO_EXTS,
    ImportFailure,
    remove_library_files,
    replace_key,
)
from app.services.sync import get_user_book, set_state
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

DISABLED_ERROR = "Downloading is not configured (DOWNLOAD_CLIENT and DOWNLOAD_URL must both be set)."


def _get_book(db: Session, book_id: int) -> Book:
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


def _grab_blocked(book: Book) -> str | None:
    """A book that is already downloading or available must not be grabbed
    again — the store is shared across users. (Adding a *new* edition goes
    through the files dialog instead.)"""
    status = book_status(book)
    if status == "downloading":
        return "This book is already downloading."
    if status == "available":
        return "This book is already available in the library."
    return None


def _replace_target(book: Book) -> Edition | None:
    """The edition whose files a replace-download swaps out."""
    return next((e for e in book.editions if e.library_path), None)


def _bitrate(path: Path) -> int | None:
    if path.suffix.lower() not in AUDIO_EXTS:
        return None
    try:
        parsed = mutagen.File(path)
        # a tag-less file is dict-like and falsy, so compare against None
        return getattr(parsed.info, "bitrate", None) if parsed is not None else None
    except Exception:
        return None


def _files_dialog(request: Request, book: Book, error: str | None = None):
    editions = []
    for edition in book.editions:
        root = Path(edition.library_path) if edition.library_path else None
        if root is None or not root.is_dir():
            continue
        files = [
            {
                "rel_path": str(p.relative_to(root)),
                "size": p.stat().st_size,
                "bitrate": _bitrate(p),
            }
            for p in sorted(root.rglob("*"))
            if p.is_file()
        ]
        editions.append({"edition": edition, "files": files})
    if not editions and not error:
        error = "This book has no downloaded files."
    return templates.TemplateResponse(
        request, "_files.html", {"book": book, "editions": editions, "error": error}
    )


@router.get("/books/{book_id}/files", response_class=HTMLResponse)
def list_files(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Details of a downloaded book's files, per edition, with rename and
    replace-download entries."""
    return _files_dialog(request, _get_book(db, book_id))


@router.post("/editions/{edition_id}/label", response_class=HTMLResponse)
def relabel(
    edition_id: int,
    request: Request,
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename an edition's label; its library folder moves to the labelled
    location immediately. Re-renders the files dialog."""
    edition = db.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="edition not found")
    book = edition.book
    try:
        relabel_edition(db, edition, label)
    except ImportFailure as exc:
        return _files_dialog(request, book, error=str(exc))
    except Exception:
        logger.exception("Relabel failed for %s", book.title)
        return _files_dialog(request, book, error="Rename failed — see the app log.")
    return _files_dialog(request, book)


@router.get("/books/{book_id}/releases", response_class=HTMLResponse)
def list_releases(
    book_id: int, request: Request, replace: bool = False, db: Session = Depends(get_db)
):
    book = _get_book(db, book_id)
    if not get_settings().downloads_enabled:
        return templates.TemplateResponse(
            request,
            "_releases.html",
            {"book": book, "releases": [], "error": DISABLED_ERROR},
        )
    replacing = replace and _replace_target(book) is not None
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
    if not get_settings().downloads_enabled:
        raise HTTPException(status_code=409, detail=DISABLED_ERROR)
    target = _replace_target(book) if replace else None
    replacing = target is not None
    blocked = _grab_blocked(book)
    if blocked and not replacing:
        raise HTTPException(status_code=409, detail=blocked)
    try:
        release = grab_release(db, user, book, guid, indexer, title, size, edition=target)
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
                remove_library_files(target)
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
