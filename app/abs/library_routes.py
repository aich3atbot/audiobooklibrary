"""ABS API: library and item catalogue endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.abs import catalogue, payloads
from app.abs.deps import require_abs_user
from app.db import get_db
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_abs_user)])


def _check_library(library_id: str) -> None:
    if library_id != payloads.LIBRARY_ID:
        raise HTTPException(status_code=404, detail="Library not found")


def _get_edition(db: Session, item_id: str):
    edition = catalogue.get_edition_by_item_id(db, item_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return edition


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
    user: User = Depends(require_abs_user),
):
    _check_library(library_id)
    filter_by = request.query_params.get("filter")
    editions = catalogue.filtered_editions(
        db, catalogue.eligible_editions(db), filter_by, user
    )
    editions = catalogue.sorted_editions(editions, sort, desc == "1", filter_by)
    total = len(editions)
    offset = page * limit if limit else 0
    if limit:
        editions = editions[offset : offset + limit]
    results = [catalogue.item_minified(e) for e in editions]
    catalogue.attach_filter_series(results, editions, filter_by)
    return {
        "results": results,
        "total": total,
        "limit": limit,
        "page": page,
        "sortBy": sort,
        "sortDesc": desc == "1",
        "filterBy": filter_by,
        "mediaType": "book",
        "minified": request.query_params.get("minified") == "1",
        "collapseseries": False,
        "include": "",
        "offset": offset,
    }


@router.get("/libraries/{library_id}/personalized")
def personalized(
    library_id: str,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    _check_library(library_id)
    return catalogue.personalized_shelves(db, user, limit)


@router.get("/libraries/{library_id}/filterdata")
def library_filterdata(library_id: str, db: Session = Depends(get_db)):
    _check_library(library_id)
    return catalogue.filterdata(db)


@router.get("/libraries/{library_id}/series")
def library_series(library_id: str, limit: int = 0, page: int = 0,
                   db: Session = Depends(get_db)):
    _check_library(library_id)
    editions = catalogue.eligible_editions(db)
    by_series: dict[int, dict] = {}
    for edition in editions:
        book = edition.book
        if book.series is None:
            continue
        group = by_series.setdefault(
            book.series_id,
            {"id": f"ser_{book.series_id}", "name": book.series.name,
             "nameIgnorePrefix": catalogue.title_prefix_at_end(book.series.name),
             "type": "series", "books": [], "addedAt": payloads.now_ms(),
             "totalDuration": 0.0},
        )
        group["books"].append(catalogue.item_minified(edition))
        group["totalDuration"] += catalogue.edition_duration(edition)
    results = sorted(by_series.values(), key=lambda s: s["name"].lower())
    total = len(results)
    if limit:
        results = results[page * limit : page * limit + limit]
    return {"results": results, "total": total, "limit": limit, "page": page,
            "sortBy": "name", "sortDesc": False, "filterBy": None,
            "minified": False, "include": ""}


def _series_payload(db: Session, series_id: str, user: User, include: str) -> dict:
    series = catalogue.get_series_by_id(db, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return catalogue.series_json(db, series, user, include)


@router.get("/libraries/{library_id}/series/{series_id}")
def library_series_detail(
    library_id: str,
    series_id: str,
    include: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    """The series page header. Clients list the books themselves through
    `/items?filter=series.<id>`; this only carries the series' own fields."""
    _check_library(library_id)
    return _series_payload(db, series_id, user, include)


@router.get("/series/{series_id}")
def series_detail(
    series_id: str,
    include: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    return _series_payload(db, series_id, user, include)


@router.get("/libraries/{library_id}/authors")
def library_authors(library_id: str, db: Session = Depends(get_db)):
    _check_library(library_id)
    counts: dict[int, dict] = {}
    for edition in catalogue.eligible_editions(db):
        book = edition.book
        entry = counts.setdefault(book.author_id, catalogue.author_entry(book))
        entry["numBooks"] += 1
    return {"authors": sorted(counts.values(), key=lambda a: a["name"].lower())}


@router.get("/authors/{author_id}")
def author(author_id: str, include: str = "", db: Session = Depends(get_db)):
    """The author landing page: `?include=items` (optionally `,series`) is how
    clients list an author's books."""
    editions = [
        e for e in catalogue.eligible_editions(db)
        if f"aut_{e.book.author_id}" == author_id
    ]
    if not editions:
        raise HTTPException(status_code=404, detail="Author not found")
    payload = catalogue.author_json(editions[0].book.author)
    includes = include.split(",")
    if "items" in includes:
        editions = catalogue.sorted_editions(editions, "media.metadata.title", False)
        if "series" in includes:
            payload["series"] = catalogue.author_series_groups(editions)
        payload["libraryItems"] = [catalogue.item_minified(e) for e in editions]
    return payload


@router.post("/items/batch/get")
async def batch_get_items(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    item_ids = body.get("libraryItemIds") or []
    if not item_ids:
        raise HTTPException(status_code=403, detail="Invalid payload")
    items = []
    for item_id in item_ids:
        edition = catalogue.get_edition_by_item_id(db, item_id)
        if edition is not None:
            items.append(catalogue.item_expanded(edition))
    return {"libraryItems": items}


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
             db: Session = Depends(get_db), user: User = Depends(require_abs_user)):
    edition = _get_edition(db, item_id)
    # Without expanded=1 ABS still returns the *full* item (audio files and
    # chapters), not the minified list shape — clients that skip the param
    # can't play a book otherwise.
    item = catalogue.item_expanded(edition) if expanded else catalogue.item_full(edition)
    if "progress" in include:
        progress = catalogue.get_progress(db, user, edition.id)
        item["userMediaProgress"] = (
            catalogue.progress_json(progress, user) if progress else None
        )
    return item


# GET /api/items/:id/cover is unauthenticated upstream and lives in
# app/abs/public_routes.py.
