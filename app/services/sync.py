"""Pull the user's library from Hardcover into the local database.

Hardcover is the source of truth for read state. Sync never touches
download_state or library_path, which are owned by the download pipeline.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import AppState, Author, Book, ReadState, Series

logger = logging.getLogger(__name__)

LAST_SYNC_KEY = "last_hardcover_sync"
LAST_SYNC_RESULT_KEY = "last_hardcover_sync_result"

# Hardcover status ids: 1=Want to Read, 2=Currently Reading, 3=Read,
# 5=Did Not Finish. Anything unmapped (e.g. DNF) becomes NONE.
STATUS_TO_READ_STATE = {
    1: ReadState.WANT_TO_READ,
    2: ReadState.READING,
    3: ReadState.READ,
}


def pick_series(book_series: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float | None]:
    """Choose one series for a book. Hardcover often lists several (e.g.
    publication vs chronological order); prefer featured, then lowest id."""
    if not book_series:
        return None, None
    ranked = sorted(book_series, key=lambda bs: (not bs.get("featured"), bs["series"]["id"]))
    best = ranked[0]
    return best["series"], best.get("position")


def parse_read_at(entry: dict[str, Any]) -> date | None:
    dates = [r["finished_at"] for r in entry.get("user_book_reads", []) if r.get("finished_at")]
    if entry.get("last_read_date"):
        dates.append(entry["last_read_date"])
    return max(date.fromisoformat(d) for d in dates) if dates else None


def sync_from_hardcover(session: Session, client: HardcoverClient) -> dict[str, int]:
    entries = client.fetch_user_books()

    authors: dict[int, Author] = {
        a.hardcover_id: a for a in session.scalars(select(Author)) if a.hardcover_id
    }
    series_map: dict[int, Series] = {
        s.hardcover_id: s for s in session.scalars(select(Series)) if s.hardcover_id
    }
    books: dict[int, Book] = {b.hardcover_id: b for b in session.scalars(select(Book))}

    created = updated = 0
    for entry in entries:
        book_data = entry["book"]
        contributions = book_data.get("contributions") or []
        author_data = contributions[0]["author"] if contributions else {"id": None, "name": "Unknown Author"}

        author = authors.get(author_data["id"])
        if author is None:
            author = Author(hardcover_id=author_data["id"], name=author_data["name"])
            session.add(author)
            if author_data["id"]:
                authors[author_data["id"]] = author
        else:
            author.name = author_data["name"]

        series_data, series_index = pick_series(book_data.get("book_series") or [])
        series = None
        if series_data:
            series = series_map.get(series_data["id"])
            if series is None:
                series = Series(hardcover_id=series_data["id"], name=series_data["name"])
                session.add(series)
                series_map[series_data["id"]] = series
            else:
                series.name = series_data["name"]

        cached_image = book_data.get("cached_image") or {}
        read_state = STATUS_TO_READ_STATE.get(entry["status_id"], ReadState.NONE)
        read_at = parse_read_at(entry)

        book = books.get(book_data["id"])
        if book is None:
            book = Book(hardcover_id=book_data["id"])
            session.add(book)
            books[book_data["id"]] = book
            created += 1
        else:
            updated += 1

        book.title = book_data["title"]
        book.author = author
        book.series = series
        book.series_index = series_index
        book.cover_url = cached_image.get("url")
        book.read_state = read_state
        book.read_at = read_at

    now = datetime.now(timezone.utc).isoformat()
    result = f"ok: {created} added, {updated} updated"
    _set_state(session, LAST_SYNC_KEY, now)
    _set_state(session, LAST_SYNC_RESULT_KEY, result)
    session.commit()
    logger.info("Hardcover sync complete: %s", result)
    return {"created": created, "updated": updated, "total": len(entries)}


def _set_state(session: Session, key: str, value: str) -> None:
    row = session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value


def get_state(session: Session, key: str) -> str | None:
    row = session.get(AppState, key)
    return row.value if row else None


def run_sync_once() -> dict[str, int]:
    """Run one pull sync with its own session and client (thread-safe)."""
    settings = get_settings()
    with HardcoverClient(settings.hardcover_token) as client:
        with get_sessionmaker()() as session:
            try:
                return sync_from_hardcover(session, client)
            except Exception as exc:
                session.rollback()
                _set_state(session, LAST_SYNC_RESULT_KEY, f"error: {exc}")
                session.commit()
                raise


async def hardcover_sync_loop() -> None:
    """Background task: pull from Hardcover on startup, then periodically."""
    settings = get_settings()
    if not settings.hardcover_token:
        logger.warning("HARDCOVER_TOKEN not set; Hardcover sync disabled")
        return
    while True:
        try:
            await asyncio.to_thread(run_sync_once)
        except Exception:
            logger.exception("Hardcover sync failed")
        await asyncio.sleep(settings.sync_interval_minutes * 60)
