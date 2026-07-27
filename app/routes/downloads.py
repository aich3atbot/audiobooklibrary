import logging
from pathlib import Path

import mutagen
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.clients.hardcover import HardcoverClient
from app.models import Book, Edition, User, book_status, display_status
from app.services.downloads import grab_release, search_releases
from app.services.editions import (
    get_or_create_edition,
    relabel_edition,
    series_edition_candidates,
    suggest_labels,
)
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
    through the book detail page instead.)"""
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
    # Hide editions the book already has (matched by Hardcover id or label) —
    # they'd only offer a duplicate download. An empty default_label never
    # matches, or every no-narrator option would vanish behind the unlabelled
    # edition.
    have_ids = {e.hardcover_edition_id for e in book.editions if e.hardcover_edition_id}
    have_labels = {e.label for e in book.editions if e.label}
    editions = [
        e
        for e in editions
        if e["id"] not in have_ids and (e["default_label"] or None) not in have_labels
    ]
    if not editions:
        return [], "All of Hardcover's audiobook editions are already downloaded — enter a label instead."
    return editions, None


def _edition_sections(
    db: Session, user: User, book: Book
) -> tuple[list[dict], list[dict], str | None]:
    """The picker's sections: sibling-series labels this book lacks (matched
    to the book's Hardcover editions by default label when possible) and the
    remaining Hardcover editions. Sibling labels survive a failed Hardcover
    fetch — they then offer the label with the sibling's narrator."""
    editions, warning = _fetch_hc_editions(user, book)
    by_label: dict[str, dict] = {}
    for e in editions:
        # first occurrence wins = highest users_count (Hardcover's fetch order)
        if e["default_label"] and e["default_label"] not in by_label:
            by_label[e["default_label"]] = e
    existing_options: list[dict] = []
    used: set[int] = set()
    for cand in series_edition_candidates(db, book):
        hc = by_label.get(cand["label"])
        if hc is not None:
            used.add(hc["id"])
            existing_options.append({"label": cand["label"], "narrator": hc["narrator"], "hc": hc})
        else:
            existing_options.append({"label": cand["label"], "narrator": cand["narrator"], "hc": None})
    rest = [e for e in editions if e["id"] not in used][:HC_EDITIONS_SHOWN]
    return existing_options, rest, warning


async def _edition_choice(request: Request) -> tuple[str, int | None, str]:
    """(label, hardcover_edition_id, narrator) from a submitted edition
    picker. A typed label overrides the picked edition's default label."""
    form = await request.form()
    label = str(form.get("edition_label") or "").strip()
    raw = str(form.get("hc_edition") or "")
    hc_id = int(raw) if raw.isdigit() else None
    narrator = str(form.get(f"hcnarr_{raw}") or "").strip() if raw else ""
    if not narrator:
        narrator = str(form.get("narrator") or "").strip()
    if not label and raw:
        label = str(form.get(f"hclabel_{raw}") or "").strip()
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


def _dir_size(library_path: str | None) -> int | None:
    """Total bytes under an edition's folder, or None when it is gone. This is
    stat-only (no mutagen), so unlike the file table it is cheap enough to run
    on page load."""
    if not library_path:
        return None
    root = Path(library_path)
    if not root.is_dir():
        return None
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            # a broken symlink or a file moving out from under us must not
            # take down the page
            continue
    return total


def _editions_context(book: Book, error: str | None = None) -> dict:
    """Context for the book detail page's editions section. Imported editions
    are listed even when their folder has gone missing, so rename/replace stay
    reachable; the lazily loaded files fragment reports the missing dir."""
    imported = [e for e in book.editions if e.library_path]
    in_flight = [
        e
        for e in book.editions
        if not e.library_path and display_status(e.download_state) != "not_present"
    ]
    return {
        "book": book,
        "imported": imported,
        "in_flight": in_flight,
        "dir_sizes": {e.id: _dir_size(e.library_path) for e in imported},
        "error": error,
    }


def _get_edition(db: Session, edition_id: int) -> Edition:
    edition = db.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="edition not found")
    return edition


def _rename_context(
    db: Session, user: User, edition: Edition, error: str | None = None
) -> dict:
    """Context for the rename overlay — the download flows' picker sections
    pointed at an edition that already exists."""
    book = edition.book
    existing_options, hc_editions, warning = _edition_sections(db, user, book)
    return {
        "book": book,
        "edition": edition,
        "existing_options": existing_options,
        "hc_editions": hc_editions,
        "warning": warning,
        "suggestions": suggest_labels(db, book),
        "error": error,
    }


def _edition_files(edition: Edition) -> list[dict]:
    root = Path(edition.library_path)
    return [
        {
            "rel_path": str(p.relative_to(root)),
            "size": p.stat().st_size,
            "bitrate": _bitrate(p),
        }
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


@router.get("/books/{book_id}", response_class=HTMLResponse)
def book_detail(book_id: int, request: Request, db: Session = Depends(get_db)):
    """The book detail page: metadata plus every edition with rename, replace
    and download-another-edition entries."""
    book = _get_book(db, book_id)
    return templates.TemplateResponse(request, "book.html", _editions_context(book))


@router.get("/editions/{edition_id}/files", response_class=HTMLResponse)
def edition_files(edition_id: int, request: Request, db: Session = Depends(get_db)):
    """An edition's file table, loaded lazily when its path is expanded —
    bitrate probing reads every audio file, too slow for page load."""
    edition = db.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="edition not found")
    missing = not (edition.library_path and Path(edition.library_path).is_dir())
    files = [] if missing else _edition_files(edition)
    return templates.TemplateResponse(
        request, "_edition_files.html", {"files": files, "missing": missing}
    )


@router.get("/editions/{edition_id}/rename", response_class=HTMLResponse)
def rename_dialog(
    edition_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The rename overlay. `_edition_sections` hides labels and Hardcover
    editions the book already holds, so the picker only ever offers a name the
    edition doesn't have."""
    edition = _get_edition(db, edition_id)
    return templates.TemplateResponse(
        request, "_edition_rename.html", _rename_context(db, user, edition)
    )


@router.post("/editions/{edition_id}/label", response_class=HTMLResponse)
async def relabel(
    edition_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rename an edition from the overlay picker; its library folder moves to
    the labelled location immediately. A picked Hardcover or sibling entry also
    re-stamps the edition's Hardcover id and narrator — the old narrator
    belonged to the old label — while a typed label leaves both alone. Closes
    the modal and swaps the detail page's editions section; a refused move
    keeps the dialog open with the reason."""
    edition = _get_edition(db, edition_id)
    book = edition.book
    label, hc_id, narrator = await _edition_choice(request)

    def dialog_error(message: str):
        return templates.TemplateResponse(
            request,
            "_edition_rename.html",
            _rename_context(db, user, edition, error=message),
        )

    try:
        relabel_edition(db, edition, label)
    except ImportFailure as exc:
        return dialog_error(str(exc))
    except Exception:
        logger.exception("Relabel failed for %s", book.title)
        return dialog_error("Rename failed — see the app log.")
    if hc_id is not None:
        edition.hardcover_edition_id = hc_id
    if narrator:
        edition.narrator = narrator
    db.commit()
    return templates.TemplateResponse(request, "_rename_done.html", _editions_context(book))


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
    existing_options, hc_editions, warning = _edition_sections(db, user, book)
    return templates.TemplateResponse(
        request,
        "_edition_options.html",
        {
            "book": book,
            "existing_options": existing_options,
            "hc_editions": hc_editions,
            "warning": warning,
            "suggestions": suggest_labels(db, book),
        },
    )


@router.get("/books/{book_id}/editions/new", response_class=HTMLResponse)
def new_edition_dialog(
    book_id: int,
    request: Request,
    detail: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Dialog for downloading another edition of an available book."""
    book = _get_book(db, book_id)
    existing_options, hc_editions, warning = _edition_sections(db, user, book)
    unlabelled = next((e for e in book.editions if e.label == ""), None)
    return templates.TemplateResponse(
        request,
        "_edition_new.html",
        {
            "book": book,
            "existing_options": existing_options,
            "hc_editions": hc_editions,
            "warning": warning,
            "suggestions": suggest_labels(db, book),
            "unlabelled": unlabelled,
            "replaceable": [e for e in book.editions if e.library_path],
            "detail": detail,
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
    from_detail = bool(form.get("detail"))
    unlabelled = next((e for e in book.editions if e.label == ""), None)

    def dialog_error(message: str):
        existing_options, hc_editions, warning = _edition_sections(db, user, book)
        return templates.TemplateResponse(
            request,
            "_edition_new.html",
            {
                "book": book,
                "existing_options": existing_options,
                "hc_editions": hc_editions,
                "warning": warning,
                "suggestions": suggest_labels(db, book),
                "unlabelled": unlabelled,
                "replaceable": [e for e in book.editions if e.library_path],
                "detail": from_detail,
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
            "from_detail": from_detail,
        },
    )


@router.get("/books/{book_id}/releases", response_class=HTMLResponse)
def list_releases(
    book_id: int,
    request: Request,
    replace: bool = False,
    edition_id: int | None = None,
    detail: bool = False,
    db: Session = Depends(get_db),
):
    book = _get_book(db, book_id)
    if not get_settings().downloads_enabled:
        return templates.TemplateResponse(
            request,
            "_releases.html",
            {"book": book, "releases": [], "error": DISABLED_ERROR, "from_detail": detail},
        )
    target = _replace_target(book, edition_id) if replace else None
    replacing = target is not None
    blocked = None if replacing else _grab_blocked(book)
    if blocked:
        return templates.TemplateResponse(
            request,
            "_releases.html",
            {"book": book, "releases": [], "error": blocked, "from_detail": detail},
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
            "from_detail": detail,
            # Siblings in the series already use labels: open the picker with
            # them on show instead of behind the "Edition (optional)" toggle.
            "expand_editions": bool(series_edition_candidates(db, book)),
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
    from_detail: bool = Form(False),
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
    if from_detail:
        # Grabbed from the book detail page — there is no card to swap, so
        # reload the page for fresh edition state.
        return Response(status_code=200, headers={"HX-Redirect": f"/books/{book.id}"})
    # Close the modal and swap the book card out-of-band so its badge updates.
    return templates.TemplateResponse(
        request,
        "_grab_done.html",
        {"book": book, "ub": get_user_book(db, user, book)},
    )
