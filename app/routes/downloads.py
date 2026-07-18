import logging
from pathlib import Path

import mutagen
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.clients.hardcover import HardcoverClient
from app.models import Book, Edition, User, book_status, display_status
from app.services.downloads import grab_release, search_releases
from app.services.editions import get_or_create_edition, relabel_edition, suggest_labels
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


def _replace_target(book: Book, edition_id: int | None = None) -> Edition | None:
    """The edition whose files a replace-download swaps out — the requested
    one, or the only sensible default when the book has a single edition."""
    candidates = [e for e in book.editions if e.library_path]
    if edition_id is not None:
        return next((e for e in candidates if e.id == edition_id), None)
    return candidates[0] if candidates else None


def _edition_blocked(edition: Edition) -> str | None:
    """Per-edition grab guard for add-another-edition grabs, where the book
    being available must not block a *new* label."""
    status = display_status(edition.download_state)
    if status == "downloading":
        return "This edition is already downloading."
    if edition.library_path:
        return "This edition is already available in the library."
    return None


HC_EDITIONS_SHOWN = 10  # books can carry dozens of junk/foreign editions


def _fetch_hc_editions(user: User, book: Book) -> tuple[list[dict], str | None]:
    """The book's Hardcover audiobook editions, or a warning when the fetch
    fails/finds nothing — the picker then degrades to a free-form label."""
    if not user.hardcover_token:
        return [], "No Hardcover token set — enter a label instead."
    try:
        with HardcoverClient(user.hardcover_token) as client:
            editions = client.fetch_editions(book.hardcover_id)
    except Exception:
        logger.exception("Hardcover editions fetch failed for %s", book.title)
        return [], "Could not fetch Hardcover's editions — enter a label instead."
    if not editions:
        return [], "Hardcover lists no audiobook editions — enter a label instead."
    return editions[:HC_EDITIONS_SHOWN], None


async def _edition_choice(request: Request) -> tuple[str, int | None, str]:
    """(label, hardcover_edition_id, narrator) from a submitted edition
    picker. A typed label overrides the picked edition's default label."""
    form = await request.form()
    label = str(form.get("edition_label") or "").strip()
    raw = str(form.get("hc_edition") or "")
    hc_id = int(raw) if raw.isdigit() else None
    narrator = str(form.get(f"hcnarr_{hc_id}") or "").strip() if hc_id else ""
    if not label and hc_id:
        label = str(form.get(f"hclabel_{hc_id}") or "").strip()
    return label, hc_id, narrator


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


@router.get("/books/{book_id}/editions/options", response_class=HTMLResponse)
def edition_options(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The edition picker fields (Hardcover editions + free label), loaded as
    its own fragment so a slow Hardcover call never blocks the release list."""
    book = _get_book(db, book_id)
    hc_editions, warning = _fetch_hc_editions(user, book)
    return templates.TemplateResponse(
        request,
        "_edition_options.html",
        {
            "book": book,
            "hc_editions": hc_editions,
            "warning": warning,
            "suggestions": suggest_labels(db, book),
        },
    )


@router.get("/books/{book_id}/editions/new", response_class=HTMLResponse)
def new_edition_dialog(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Dialog for downloading another edition of an available book."""
    book = _get_book(db, book_id)
    hc_editions, warning = _fetch_hc_editions(user, book)
    unlabelled = next((e for e in book.editions if e.label == ""), None)
    return templates.TemplateResponse(
        request,
        "_edition_new.html",
        {
            "book": book,
            "hc_editions": hc_editions,
            "warning": warning,
            "suggestions": suggest_labels(db, book),
            "unlabelled": unlabelled,
            "error": None,
        },
    )


@router.post("/books/{book_id}/editions/new", response_class=HTMLResponse)
async def new_edition_submit(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Validate the new edition's label (relabelling the existing unlabelled
    edition first — no unsuffixed edition once there are two), then show the
    release picker carrying the choice."""
    book = _get_book(db, book_id)
    form = await request.form()
    label, hc_id, narrator = await _edition_choice(request)
    existing_label = str(form.get("existing_label") or "").strip()
    unlabelled = next((e for e in book.editions if e.label == ""), None)

    def dialog_error(message: str):
        hc_editions, warning = _fetch_hc_editions(user, book)
        return templates.TemplateResponse(
            request,
            "_edition_new.html",
            {
                "book": book,
                "hc_editions": hc_editions,
                "warning": warning,
                "suggestions": suggest_labels(db, book),
                "unlabelled": unlabelled,
                "error": message,
            },
        )

    if not label:
        return dialog_error("Pick a Hardcover edition or enter a label.")
    if unlabelled is not None:
        if not existing_label:
            return dialog_error(
                "The existing files need a label too, so both editions get named folders."
            )
        if existing_label == label:
            return dialog_error("The new edition needs a different label than the existing files.")
    target = next((e for e in book.editions if e.label == label), None)
    if target is not None and (blocked := _edition_blocked(target)):
        return dialog_error(blocked)
    if unlabelled is not None:
        try:
            relabel_edition(db, unlabelled, existing_label)
        except ImportFailure as exc:
            return dialog_error(str(exc))
        except Exception:
            logger.exception("Relabel failed for %s", book.title)
            return dialog_error("Renaming the existing edition failed — see the app log.")

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
        {
            "book": book,
            "releases": releases,
            "error": error,
            "add_edition": True,
            "edition_label": label,
            "hardcover_edition_id": hc_id,
            "narrator": narrator,
        },
    )


@router.get("/books/{book_id}/releases", response_class=HTMLResponse)
def list_releases(
    book_id: int,
    request: Request,
    replace: bool = False,
    edition_id: int | None = None,
    db: Session = Depends(get_db),
):
    book = _get_book(db, book_id)
    if not get_settings().downloads_enabled:
        return templates.TemplateResponse(
            request,
            "_releases.html",
            {"book": book, "releases": [], "error": DISABLED_ERROR},
        )
    target = _replace_target(book, edition_id) if replace else None
    replacing = target is not None
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
        {
            "book": book,
            "releases": releases,
            "error": error,
            "replace": replacing,
            "replace_edition_id": target.id if target else None,
        },
    )


@router.post("/books/{book_id}/grab", response_class=HTMLResponse)
async def grab(
    book_id: int,
    request: Request,
    guid: str = Form(...),
    indexer: str = Form(...),
    title: str = Form(...),
    size: int | None = Form(None),
    replace: bool = Form(False),
    add_edition: bool = Form(False),
    edition_id: int | None = Form(None),
    remove: str = Form("after_import"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    book = _get_book(db, book_id)
    if not get_settings().downloads_enabled:
        raise HTTPException(status_code=409, detail=DISABLED_ERROR)
    label, hc_id, narrator = await _edition_choice(request)
    target = _replace_target(book, edition_id) if replace else None
    replacing = target is not None
    if replacing:
        edition = target
    elif add_edition:
        # A new edition of an available book: the book-level guard must not
        # apply, but the label's own edition must be idle — and it must be a
        # label, or the new edition would land in the unsuffixed folder.
        if not label:
            raise HTTPException(status_code=409, detail="An additional edition needs a label.")
        edition = get_or_create_edition(db, book, label, hc_id, narrator)
        if blocked := _edition_blocked(edition):
            raise HTTPException(status_code=409, detail=blocked)
    else:
        if blocked := _grab_blocked(book):
            raise HTTPException(status_code=409, detail=blocked)
        # First grab may optionally carry an edition choice; "" is the
        # unlabelled edition (today's behavior).
        edition = get_or_create_edition(db, book, label, hc_id, narrator)
    try:
        release = grab_release(db, user, book, guid, indexer, title, size, edition=edition)
    except Exception:
        logger.exception("Grab failed for %s", book.title)
        db.rollback()
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
