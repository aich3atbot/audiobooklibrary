import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import get_current_user
from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.db import get_db
from app.models import Book, User
from app.services.collection import (
    HIGH_CONFIDENCE,
    annotate_edition_hints,
    cache_match,
    cached_match,
    clear_cached_match,
    ensure_book,
    entry_duration,
    entry_for,
    fill_slugs_from_book,
    find_local_matches,
    identify_entry,
    import_entry,
    match_from_book,
    match_is_stale,
    match_from_result,
    scan_imports,
)
from app.services.editions import (
    edition_choice,
    edition_sections,
    ns_field,
    suggest_labels,
)
from app.services.importer import ImportFailure
from app.services.sync import run_sync_all
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _row(db: Session, entry, match, error=None) -> dict:
    if match is None:
        confidence = None
    elif match.get("score") is None:
        confidence = "manual"
    elif match["score"] >= HIGH_CONFIDENCE:
        confidence = "high"
    else:
        confidence = "low"
    # a Book that already exists locally for this match (may be "available")
    book = None
    label_suggestions: list[str] = []
    if match is not None:
        book = db.scalar(select(Book).where(Book.hardcover_id == match["hardcover_id"]))
        fill_slugs_from_book(match, book)
    if book is not None:
        # series-mates' labels for the edition-label input; a label this book
        # already has imported would be refused, so don't suggest it
        taken = {e.label for e in book.editions if e.library_path}
        label_suggestions = [s for s in suggest_labels(db, book) if s not in taken]
    return {"entry": entry, "match": match, "book": book,
            "label_suggestions": label_suggestions,
            "confidence": confidence, "error": error}


def _identify(db: Session, user: User, entry) -> tuple[dict | None, str | None]:
    """Cached Hardcover identification for one entry; identifies and caches
    on a miss. A cached null (identification ran, found nothing) does not
    re-hit Hardcover on every page load. Returns (match, error)."""
    from app.services.collection import MATCH_CACHE_PREFIX
    from app.services.sync import get_state

    cached = None
    if get_state(db, MATCH_CACHE_PREFIX + entry.rel) is not None:
        cached = cached_match(db, entry)
        # a stale match falls through to re-identification, which re-caches it
        # in the current shape; it stays usable meanwhile — only its
        # hardcover.app link is missing
        if not match_is_stale(cached):
            return cached, None
    if not user.hardcover_token:
        return cached, None if cached else "no Hardcover token on your account"
    try:
        with HardcoverClient(user.hardcover_token) as client:
            match = identify_entry(client, entry)
    except Exception:
        logger.exception("Hardcover identification failed for %s", entry.rel)
        return cached, "Hardcover identification failed"
    cache_match(db, entry.rel, match)
    return match, None


def _scan_rows(db: Session, user: User, errors: dict[str, str] | None = None) -> list[dict]:
    errors = errors or {}
    rows = []
    for entry in scan_imports():
        match, ident_error = _identify(db, user, entry)
        rows.append(_row(db, entry, match, errors.get(entry.rel) or ident_error))
    return rows


def _schedule_sync_all() -> None:
    """Kick a background sync of every user so a book just imported shows up
    immediately for users who have it in their Hardcover library."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(asyncio.to_thread(run_sync_all))


@router.get("/imports", response_class=HTMLResponse)
async def imports_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    available = get_settings().imports_dir.is_dir()
    rows = await run_in_threadpool(_scan_rows, db, user) if available else []
    return templates.TemplateResponse(
        request, "imports.html", {"available": available, "rows": rows}
    )


@router.get("/imports/search", response_class=HTMLResponse)
def match_search(request: Request, rel: str, q: str = "", db: Session = Depends(get_db)):
    books = find_local_matches(db, q) if q.strip() else []
    return templates.TemplateResponse(
        request, "_match_options.html", {"rel": rel, "q": q.strip(), "books": books}
    )


@router.get("/imports/hardcover", response_class=HTMLResponse)
def match_hardcover(
    request: Request,
    rel: str,
    q: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    error = None
    results = []
    try:
        with HardcoverClient(user.hardcover_token) as client:
            results = client.search_books(q.strip(), per_page=5)
    except Exception:
        logger.exception("Hardcover search failed for imports match")
        error = "Hardcover search failed."
    return templates.TemplateResponse(
        request,
        "_hardcover_options.html",
        {"rel": rel, "results": results, "error": error},
    )


@router.get("/imports/editions", response_class=HTMLResponse)
async def edition_picker(
    request: Request,
    rel: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The edition picker for one import row, loaded when its disclosure is
    opened — it costs a Hardcover editions fetch plus a header read of every
    audio file, far too much to do for every row on page load. Fields are
    namespaced by `rel` because the Import selected/all buttons post the whole
    table in one request."""
    entry = entry_for(rel)
    if entry is None:
        raise HTTPException(status_code=404, detail="import entry no longer exists")
    match = cached_match(db, entry)

    def build() -> dict:
        if match is None:
            return {"existing_options": [], "hc_editions": [], "suggestions": [],
                    "warning": "Match this entry to a book first."}
        book = db.scalar(select(Book).where(Book.hardcover_id == match["hardcover_id"]))
        existing_options, hc_editions, warning = edition_sections(
            db, user, match["hardcover_id"], book
        )
        annotate_edition_hints(
            hc_editions + [o["hc"] for o in existing_options if o["hc"]],
            entry,
            entry_duration(entry),
        )
        return {
            "existing_options": existing_options,
            "hc_editions": hc_editions,
            "warning": warning,
            "suggestions": suggest_labels(db, book) if book is not None else [],
        }

    context = await run_in_threadpool(build)
    return templates.TemplateResponse(
        request,
        "_edition_options.html",
        {
            **context,
            "field_ns": rel,
            "options_id": f"edition-options-{entry.row_id}",
            "label_target": f"edlabel-{entry.row_id}",
            # nothing is being downloaded here — the headings name what the
            # files already are
            "existing_heading": "Editions used elsewhere in this series",
            "new_heading": "Hardcover's audiobook editions",
            # the row owns the label input, so the picker only offers choices
            "omit_label": True,
            "collapse_new": False,
        },
    )


@router.post("/imports/set-match", response_class=HTMLResponse)
def set_match(
    request: Request, rel: str = Form(...), book_id: int = Form(...),
    db: Session = Depends(get_db),
):
    entry = entry_for(rel)
    if entry is None:
        raise HTTPException(status_code=404, detail="import entry no longer exists")
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    match = match_from_book(book)
    cache_match(db, rel, match)
    return templates.TemplateResponse(
        request, "_import_row.html", {"m": _row(db, entry, match)}
    )


@router.post("/imports/set-hardcover-match", response_class=HTMLResponse)
def set_hardcover_match(
    request: Request,
    rel: str = Form(...),
    hardcover_id: int = Form(...),
    title: str = Form(...),
    slug: str = Form(""),
    author: str = Form(""),
    series_name: str = Form(""),
    series_slug: str = Form(""),
    series_position: float | None = Form(None),
    db: Session = Depends(get_db),
):
    entry = entry_for(rel)
    if entry is None:
        raise HTTPException(status_code=404, detail="import entry no longer exists")
    match = match_from_result(
        {
            "hardcover_id": hardcover_id,
            "slug": slug or None,
            "title": title,
            "authors": [author] if author else [],
            "series_name": series_name or None,
            "series_position": series_position,
            "series_slug": series_slug or None,
        },
        score=None,  # manual choice
    )
    cache_match(db, rel, match)
    return templates.TemplateResponse(
        request, "_import_row.html", {"m": _row(db, entry, match)}
    )


@router.post("/imports/reidentify", response_class=HTMLResponse)
def reidentify(
    request: Request,
    rel: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = entry_for(rel)
    if entry is None:
        raise HTTPException(status_code=404, detail="import entry no longer exists")
    clear_cached_match(db, rel)
    match, error = _identify(db, user, entry)
    return templates.TemplateResponse(
        request, "_import_row.html", {"m": _row(db, entry, match, error)}
    )


@router.post("/imports/import", response_class=HTMLResponse)
async def run_import(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    form = await request.form()
    mode = form.get("mode", "one")
    match_map = {
        key[len("hc__"):]: int(value)
        for key, value in form.multi_items()
        if key.startswith("hc__") and value
    }
    if mode == "one":
        rels = [form["rel"]] if form.get("rel") else []
    elif mode == "selected":
        rels = form.getlist("sel")
    elif mode == "all":
        rels = list(match_map)
    else:
        raise HTTPException(status_code=422, detail=f"unknown mode: {mode}")

    def process() -> tuple[dict[str, str], int]:
        errors: dict[str, str] = {}
        imported = 0
        for rel in rels:
            entry = entry_for(rel)
            if entry is None:
                errors[rel] = "no longer exists in /imports"
                continue
            hardcover_id = match_map.get(rel)
            if not hardcover_id:
                errors[rel] = "no match selected"
                continue
            try:
                with HardcoverClient(user.hardcover_token) as client:
                    book = ensure_book(db, client, hardcover_id)
                # The row's edition picker, if it was opened: a chosen edition
                # stamps its Hardcover id and narrator onto the edition row.
                # The row always submits its label box (prefilled from the
                # pick, but editable), so an empty one there means the user
                # cleared it — asking for the plain unsuffixed folder. Only a
                # form that omits the field falls back to the pick's label.
                label, hc_edition_id, narrator = edition_choice(
                    form, ns=rel, pick_sets_label=ns_field("edition_label", rel) not in form
                )
                import_entry(
                    db,
                    book,
                    entry,
                    label=label,
                    hardcover_edition_id=hc_edition_id,
                    narrator=narrator,
                )
                clear_cached_match(db, rel)
                imported += 1
            except ImportFailure as exc:
                errors[rel] = str(exc)
            except Exception as exc:
                logger.exception("Collection import failed for %s", rel)
                errors[rel] = f"unexpected error: {exc}"
        return errors, imported

    errors, imported = await run_in_threadpool(process)
    if imported:
        _schedule_sync_all()
    rows = await run_in_threadpool(_scan_rows, db, user, errors)
    return templates.TemplateResponse(
        request, "_imports_table.html", {"rows": rows, "available": True}
    )
