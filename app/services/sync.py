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

from app.clients.hardcover import HardcoverClient, HardcoverError
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
READ_STATE_TO_STATUS = {v: k for k, v in STATUS_TO_READ_STATE.items()}


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


Caches = tuple[dict[int, Author], dict[int, Series], dict[int, Book]]


def _load_caches(session: Session) -> Caches:
    authors = {a.hardcover_id: a for a in session.scalars(select(Author)) if a.hardcover_id}
    series_map = {s.hardcover_id: s for s in session.scalars(select(Series)) if s.hardcover_id}
    books = {b.hardcover_id: b for b in session.scalars(select(Book))}
    return authors, series_map, books


def _upsert_entry(session: Session, entry: dict[str, Any], caches: Caches) -> tuple[Book, bool]:
    """Upsert one user_book entry; returns (book, created)."""
    authors, series_map, books = caches
    book_data = entry["book"]
    contributions = book_data.get("contributions") or []
    author_data = (
        contributions[0]["author"] if contributions else {"id": None, "name": "Unknown Author"}
    )

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

    book = books.get(book_data["id"])
    created = book is None
    if created:
        book = Book(hardcover_id=book_data["id"])
        session.add(book)
        books[book_data["id"]] = book

    book.title = book_data["title"]
    book.author = author
    book.series = series
    book.series_index = series_index
    book.cover_url = cached_image.get("url")
    book.hardcover_user_book_id = entry["id"]
    # A pending local change hasn't reached Hardcover yet; don't clobber
    # it with the stale remote state (push happens before pull anyway).
    if not book.pending_push:
        book.read_state = STATUS_TO_READ_STATE.get(entry["status_id"], ReadState.NONE)
        book.read_at = parse_read_at(entry)
    return book, created


def sync_from_hardcover(session: Session, client: HardcoverClient) -> dict[str, int]:
    entries = client.fetch_user_books()
    caches = _load_caches(session)

    created = updated = 0
    for entry in entries:
        _, was_created = _upsert_entry(session, entry, caches)
        if was_created:
            created += 1
        else:
            updated += 1

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


def push_book(session: Session, client: HardcoverClient, book: Book) -> None:
    """Push one book's read state to Hardcover; clears pending_push on success."""
    if book.read_state == ReadState.NONE:
        if book.hardcover_user_book_id:
            client.delete_user_book(book.hardcover_user_book_id)
            book.hardcover_user_book_id = None
    else:
        status_id = READ_STATE_TO_STATUS[book.read_state]
        last_read = (
            book.read_at.isoformat()
            if book.read_at and book.read_state == ReadState.READ
            else None
        )
        if book.hardcover_user_book_id:
            client.update_user_book(book.hardcover_user_book_id, status_id, last_read)
        else:
            book.hardcover_user_book_id = client.insert_user_book(
                book.hardcover_id, status_id, last_read
            )
    book.pending_push = False
    session.commit()


def push_pending(session: Session, client: HardcoverClient) -> int:
    """Retry unconfirmed local changes; returns how many were pushed."""
    pending = session.scalars(select(Book).where(Book.pending_push)).all()
    pushed = 0
    for book in pending:
        try:
            push_book(session, client, book)
            pushed += 1
        except Exception:
            session.rollback()
            logger.exception("Failed to push read state for book %s", book.hardcover_id)
    return pushed


def update_read_state(session: Session, book: Book, new_state: ReadState) -> Book:
    """Apply a UI read-state change locally, then try to push it to Hardcover.

    On push failure the book stays pending_push and the sync loop retries."""
    book.read_state = new_state
    if new_state == ReadState.READ:
        book.read_at = book.read_at or date.today()
    elif new_state == ReadState.NONE:
        book.read_at = None
    book.pending_push = True
    session.commit()

    settings = get_settings()
    if not settings.hardcover_token:
        return book
    try:
        with HardcoverClient(settings.hardcover_token) as client:
            push_book(session, client, book)
    except Exception:
        session.rollback()
        logger.exception(
            "Push to Hardcover failed for book %s; will retry on next sync", book.hardcover_id
        )
    return book


def sync_one_book(session: Session, client: HardcoverClient, hardcover_book_id: int) -> Book | None:
    """Pull one book's shelf entry from Hardcover into the local database."""
    entry = client.fetch_user_book(hardcover_book_id)
    if entry is None:
        return None
    book, _ = _upsert_entry(session, entry, _load_caches(session))
    session.commit()
    return book


def add_book(session: Session, hardcover_book_id: int, state: ReadState) -> Book:
    """Add a searched book: shelve it on Hardcover, then pull its metadata."""
    existing = session.scalar(select(Book).where(Book.hardcover_id == hardcover_book_id))
    if existing:
        return update_read_state(session, existing, state)

    settings = get_settings()
    with HardcoverClient(settings.hardcover_token) as client:
        status_id = READ_STATE_TO_STATUS[state]
        last_read = date.today().isoformat() if state == ReadState.READ else None
        client.insert_user_book(hardcover_book_id, status_id, last_read)
        book = sync_one_book(session, client, hardcover_book_id)
    if book is None:
        raise HardcoverError(
            f"book {hardcover_book_id} not on Hardcover shelf after insert"
        )
    return book


def run_sync_once() -> dict[str, int]:
    """Run one sync (push pending, then pull) with its own session and client."""
    settings = get_settings()
    with HardcoverClient(settings.hardcover_token) as client:
        with get_sessionmaker()() as session:
            try:
                push_pending(session, client)
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
