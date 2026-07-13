import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.db import get_db
from app.models import Book, ReadState
from app.services.collection import (
    HIGH_CONFIDENCE,
    best_matches,
    entry_for,
    find_local_matches,
    import_entry,
    scan_imports,
)
from app.services.importer import ImportFailure
from app.services.sync import add_book
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

ADDABLE_STATES = (ReadState.WANT_TO_READ, ReadState.READING, ReadState.READ)


def _row(entry, book, score=None, error=None) -> dict:
    if book is None:
        confidence = None
    elif score is None:
        confidence = "manual"
    elif score >= HIGH_CONFIDENCE:
        confidence = "high"
    else:
        confidence = "low"
    return {"entry": entry, "book": book, "score": score, "confidence": confidence,
            "error": error}


def _scan_rows(db: Session, keep: dict[str, int] | None = None,
               errors: dict[str, str] | None = None) -> list[dict]:
    """Scan and match, preserving amended matches (keep: rel -> book id)."""
    keep = keep or {}
    errors = errors or {}
    rows = []
    for match in best_matches(db, scan_imports()):
        entry = match["entry"]
        if entry.rel in keep:
            book = db.get(Book, keep[entry.rel])
            rows.append(_row(entry, book, None, errors.get(entry.rel)))
        else:
            rows.append(
                _row(entry, match["book"], match["score"] or None, errors.get(entry.rel))
            )
    return rows


@router.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)):
    available = get_settings().imports_dir.is_dir()
    rows = _scan_rows(db) if available else []
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
def match_hardcover(request: Request, rel: str, q: str, db: Session = Depends(get_db)):
    error = None
    results = []
    try:
        with HardcoverClient(get_settings().hardcover_token) as client:
            results = client.search_books(q.strip(), per_page=5)
    except Exception:
        logger.exception("Hardcover search failed for imports match")
        error = "Hardcover search failed."
    return templates.TemplateResponse(
        request,
        "_hardcover_options.html",
        {"rel": rel, "results": results, "error": error},
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
    return templates.TemplateResponse(request, "_import_row.html", {"m": _row(entry, book)})


@router.post("/imports/add-match", response_class=HTMLResponse)
def add_and_match(
    request: Request,
    rel: str = Form(...),
    hardcover_id: int = Form(...),
    state: str = Form(...),
    db: Session = Depends(get_db),
):
    entry = entry_for(rel)
    if entry is None:
        raise HTTPException(status_code=404, detail="import entry no longer exists")
    try:
        read_state = ReadState(state)
    except ValueError:
        read_state = None
    if read_state not in ADDABLE_STATES:
        raise HTTPException(status_code=422, detail=f"invalid state: {state}")
    book = add_book(db, hardcover_id, read_state)
    return templates.TemplateResponse(request, "_import_row.html", {"m": _row(entry, book)})


@router.post("/imports/import", response_class=HTMLResponse)
async def run_import(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    mode = form.get("mode", "one")
    book_map = {
        key[len("book__"):]: int(value)
        for key, value in form.multi_items()
        if key.startswith("book__") and value
    }
    if mode == "one":
        rels = [form["rel"]] if form.get("rel") else []
    elif mode == "selected":
        rels = form.getlist("sel")
    elif mode == "all":
        rels = list(book_map)
    else:
        raise HTTPException(status_code=422, detail=f"unknown mode: {mode}")

    def process() -> dict[str, str]:
        errors: dict[str, str] = {}
        for rel in rels:
            entry = entry_for(rel)
            if entry is None:
                errors[rel] = "no longer exists in /imports"
                continue
            book = db.get(Book, book_map.get(rel, -1))
            if book is None:
                errors[rel] = "no book selected"
                continue
            try:
                import_entry(db, book, entry)
            except ImportFailure as exc:
                errors[rel] = str(exc)
            except Exception as exc:
                logger.exception("Collection import failed for %s", rel)
                errors[rel] = f"unexpected error: {exc}"
        return errors

    errors = await run_in_threadpool(process)
    keep = {rel: book_id for rel, book_id in book_map.items() if rel not in rels or rel in errors}
    rows = _scan_rows(db, keep=keep, errors=errors)
    return templates.TemplateResponse(
        request, "_imports_table.html", {"rows": rows, "available": True}
    )
