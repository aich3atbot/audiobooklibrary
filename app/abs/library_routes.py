"""ABS API: library and item catalogue endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.abs import catalogue, payloads
from app.abs.deps import require_abs_user
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_abs_user)])


def _check_library(library_id: str) -> None:
    if library_id != payloads.LIBRARY_ID:
        raise HTTPException(status_code=404, detail="Library not found")


def _get_book(db: Session, item_id: str):
    book = catalogue.get_book_by_item_id(db, item_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return book


@router.get("/libraries")
def libraries():
    return {"libraries": [catalogue.library_json()]}


@router.get("/libraries/{library_id}")
def library(library_id: str, include: str = "", db: Session = Depends(get_db)):
    _check_library(library_id)
    lib = catalogue.library_json()
    if "filterdata" in include:
        return {
            "filterdata": catalogue.filterdata(db),
            "issues": 0,
            "numUserPlaylists": 0,
            "library": lib,
        }
    return lib


@router.get("/libraries/{library_id}/items")
def library_items(
    library_id: str,
    request: Request,
    limit: int = 0,
    page: int = 0,
    sort: str | None = None,
    desc: str = "0",
    db: Session = Depends(get_db),
):
    _check_library(library_id)
    books = catalogue.sorted_books(catalogue.eligible_books(db), sort, desc == "1")
    total = len(books)
    offset = page * limit if limit else 0
    if limit:
        books = books[offset : offset + limit]
    return {
        "results": [catalogue.item_minified(b) for b in books],
        "total": total,
        "limit": limit,
        "page": page,
        "sortBy": sort,
        "sortDesc": desc == "1",
        "filterBy": request.query_params.get("filter"),
        "mediaType": "book",
        "minified": request.query_params.get("minified") == "1",
        "collapseseries": False,
        "include": "",
        "offset": offset,
    }


@router.get("/libraries/{library_id}/personalized")
def personalized(library_id: str, limit: int = 10, db: Session = Depends(get_db)):
    _check_library(library_id)
    return catalogue.personalized_shelves(db, limit)


@router.get("/libraries/{library_id}/filterdata")
def library_filterdata(library_id: str, db: Session = Depends(get_db)):
    _check_library(library_id)
    return catalogue.filterdata(db)


@router.get("/libraries/{library_id}/series")
def library_series(library_id: str, limit: int = 0, page: int = 0,
                   db: Session = Depends(get_db)):
    _check_library(library_id)
    books = catalogue.eligible_books(db)
    by_series: dict[int, dict] = {}
    for book in books:
        if book.series is None:
            continue
        group = by_series.setdefault(
            book.series_id,
            {"id": f"ser_{book.series_id}", "name": book.series.name,
             "nameIgnorePrefix": catalogue.title_prefix_at_end(book.series.name),
             "type": "series", "books": [], "addedAt": payloads.now_ms(),
             "totalDuration": 0.0},
        )
        group["books"].append(catalogue.item_minified(book))
        group["totalDuration"] += catalogue.book_duration(book)
    results = sorted(by_series.values(), key=lambda s: s["name"].lower())
    total = len(results)
    if limit:
        results = results[page * limit : page * limit + limit]
    return {"results": results, "total": total, "limit": limit, "page": page,
            "sortBy": "name", "sortDesc": False, "filterBy": None,
            "minified": False, "include": ""}


@router.get("/libraries/{library_id}/authors")
def library_authors(library_id: str, db: Session = Depends(get_db)):
    _check_library(library_id)
    counts: dict[int, dict] = {}
    for book in catalogue.eligible_books(db):
        entry = counts.setdefault(book.author_id, catalogue.author_entry(book))
        entry["numBooks"] += 1
    return {"authors": sorted(counts.values(), key=lambda a: a["name"].lower())}


@router.get("/libraries/{library_id}/search")
def library_search(library_id: str, q: str = "", limit: int = 12,
                   db: Session = Depends(get_db)):
    _check_library(library_id)
    if not q.strip():
        raise HTTPException(status_code=400, detail='Query param "q" must be a string')
    return catalogue.search_library(db, q, limit)


@router.get("/libraries/{library_id}/playlists")
def library_playlists(library_id: str, limit: int = 0, page: int = 0):
    # Playlists aren't supported; empty result keeps the app's tab rendering.
    _check_library(library_id)
    return {"results": [], "total": 0, "limit": limit, "page": page}


@router.get("/libraries/{library_id}/collections")
def library_collections(library_id: str, limit: int = 0, page: int = 0):
    # Collections aren't supported; empty result keeps the app's tab rendering.
    _check_library(library_id)
    return {"results": [], "total": 0, "limit": limit, "page": page,
            "sortBy": None, "sortDesc": False, "filterBy": None,
            "minified": False, "include": ""}


@router.get("/items/{item_id}")
def get_item(item_id: str, expanded: int = 0, include: str = "",
             db: Session = Depends(get_db)):
    book = _get_book(db, item_id)
    item = catalogue.item_expanded(book) if expanded else catalogue.item_minified(book)
    if "progress" in include:
        item["userMediaProgress"] = (
            catalogue.progress_json(book.media_progress) if book.media_progress else None
        )
    return item


@router.get("/items/{item_id}/cover")
def item_cover(item_id: str, db: Session = Depends(get_db)):
    book = _get_book(db, item_id)
    cover = catalogue.find_cover_file(book)
    if cover is not None:
        return FileResponse(cover)
    if book.cover_url:
        return RedirectResponse(url=book.cover_url, status_code=302)
    raise HTTPException(status_code=404, detail="No cover")
