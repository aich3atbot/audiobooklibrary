"""Pull each user's library from Hardcover into the local database.

Hardcover is the source of truth for read state. Every enabled user with a
token syncs with their own Hardcover account: shared Book/Author/Series
metadata rows are upserted (last writer wins), and the user's shelf
membership and read state live on their UserBook rows. Sync never touches
download_state or library_path, which are owned by the download pipeline.
"""

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.hardcover import HardcoverClient, HardcoverError
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import AppState, Author, Book, ReadState, Series, User, UserBook

logger = logging.getLogger(__name__)

# Pause between users in a sync pass — politeness to the beta API.
USER_SYNC_PAUSE_SECONDS = 2.0

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


def _load_user_books(session: Session, user: User) -> dict[int, UserBook]:
    """The user's shelf rows keyed by the book's hardcover_id."""
    rows = session.scalars(
        select(UserBook).join(UserBook.book).where(UserBook.user_id == user.id)
    )
    return {ub.book.hardcover_id: ub for ub in rows}


def upsert_book_metadata(session: Session, book_data: dict[str, Any], caches: Caches) -> Book:
    """Upsert the shared Book/Author/Series metadata for one Hardcover book
    document (the inner ``book`` shape). Used by sync and by imports."""
    authors, series_map, books = caches
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
    if book is None:
        book = Book(hardcover_id=book_data["id"])
        session.add(book)
        books[book_data["id"]] = book

    book.title = book_data["title"]
    book.author = author
    book.series = series
    book.series_index = series_index
    book.cover_url = cached_image.get("url")
    return book


def _upsert_entry(
    session: Session,
    entry: dict[str, Any],
    caches: Caches,
    user: User,
    user_books: dict[int, UserBook],
) -> tuple[Book, bool]:
    """Upsert one of the user's Hardcover shelf entries; returns
    (book, added_to_library)."""
    book = upsert_book_metadata(session, entry["book"], caches)

    ub = user_books.get(book.hardcover_id)
    added = ub is None
    if added:
        ub = UserBook(user=user, book=book)
        session.add(ub)
        user_books[book.hardcover_id] = ub

    ub.hardcover_user_book_id = entry["id"]
    # A pending local change hasn't reached Hardcover yet; don't clobber
    # it with the stale remote state (push happens before pull anyway).
    if not ub.pending_push:
        ub.read_state = STATUS_TO_READ_STATE.get(entry["status_id"], ReadState.NONE)
        ub.read_at = parse_read_at(entry)
    return book, added


def sync_from_hardcover(session: Session, client: HardcoverClient, user: User) -> dict[str, int]:
    entries = client.fetch_user_books()
    caches = _load_caches(session)
    user_books = _load_user_books(session, user)

    added = updated = 0
    for entry in entries:
        _, was_added = _upsert_entry(session, entry, caches, user, user_books)
        if was_added:
            added += 1
        else:
            updated += 1

    user.last_sync_at = datetime.now(timezone.utc).replace(tzinfo=None)
    user.last_sync_result = f"ok: {added} added, {updated} updated"
    session.commit()
    logger.info("Hardcover sync complete for %s: %s", user.username, user.last_sync_result)
    return {"created": added, "updated": updated, "total": len(entries)}


def _set_state(session: Session, key: str, value: str) -> None:
    row = session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value


def get_state(session: Session, key: str) -> str | None:
    row = session.get(AppState, key)
    return row.value if row else None


def get_user_book(session: Session, user: User, book: Book) -> UserBook | None:
    return session.scalar(
        select(UserBook).where(UserBook.user_id == user.id, UserBook.book_id == book.id)
    )


def push_book(session: Session, client: HardcoverClient, user_book: UserBook) -> None:
    """Push one shelf entry's read state to the owning user's Hardcover;
    clears pending_push on success."""
    if user_book.read_state == ReadState.NONE:
        if user_book.hardcover_user_book_id:
            client.delete_user_book(user_book.hardcover_user_book_id)
            user_book.hardcover_user_book_id = None
    else:
        status_id = READ_STATE_TO_STATUS[user_book.read_state]
        last_read = (
            user_book.read_at.isoformat()
            if user_book.read_at and user_book.read_state == ReadState.READ
            else None
        )
        if user_book.hardcover_user_book_id:
            client.update_user_book(user_book.hardcover_user_book_id, status_id, last_read)
        else:
            user_book.hardcover_user_book_id = client.insert_user_book(
                user_book.book.hardcover_id, status_id, last_read
            )
    user_book.pending_push = False
    session.commit()


def push_pending(session: Session, client: HardcoverClient, user: User) -> int:
    """Retry the user's unconfirmed local changes; returns how many pushed."""
    pending = session.scalars(
        select(UserBook).where(UserBook.user_id == user.id, UserBook.pending_push)
    ).all()
    pushed = 0
    for user_book in pending:
        try:
            push_book(session, client, user_book)
            pushed += 1
        except Exception:
            session.rollback()
            logger.exception(
                "Failed to push read state for book %s (user %s)",
                user_book.book.hardcover_id,
                user.username,
            )
    return pushed


def update_read_state(session: Session, user: User, book: Book, new_state: ReadState) -> UserBook:
    """Apply a read-state change for one user locally, then try to push it to
    their Hardcover. Creates the shelf membership if the book wasn't in the
    user's library yet (the "available → add to my library" case).

    On push failure the entry stays pending_push and the sync loop retries."""
    user_book = get_user_book(session, user, book)
    if user_book is None:
        user_book = UserBook(user=user, book=book)
        session.add(user_book)

    user_book.read_state = new_state
    if new_state == ReadState.READ:
        user_book.read_at = user_book.read_at or date.today()
    elif new_state == ReadState.NONE:
        user_book.read_at = None
    user_book.pending_push = True
    session.commit()

    if not user.hardcover_token:
        return user_book
    try:
        with HardcoverClient(user.hardcover_token) as client:
            push_book(session, client, user_book)
    except Exception:
        session.rollback()
        logger.exception(
            "Push to Hardcover failed for book %s (user %s); will retry on next sync",
            book.hardcover_id,
            user.username,
        )
    return user_book


def sync_one_book(
    session: Session, client: HardcoverClient, user: User, hardcover_book_id: int
) -> Book | None:
    """Pull one of the user's shelf entries from Hardcover into the database."""
    entry = client.fetch_user_book(hardcover_book_id)
    if entry is None:
        return None
    book, _ = _upsert_entry(
        session, entry, _load_caches(session), user, _load_user_books(session, user)
    )
    session.commit()
    return book


def add_book(session: Session, user: User, hardcover_book_id: int, state: ReadState) -> Book:
    """Add a searched book to the user's library: shelve it on their
    Hardcover, then pull its metadata. A book already known locally (even one
    another user made available) just gets the user's shelf entry."""
    existing = session.scalar(select(Book).where(Book.hardcover_id == hardcover_book_id))
    if existing is not None:
        update_read_state(session, user, existing, state)
        return existing

    with HardcoverClient(user.hardcover_token) as client:
        status_id = READ_STATE_TO_STATUS[state]
        last_read = date.today().isoformat() if state == ReadState.READ else None
        client.insert_user_book(hardcover_book_id, status_id, last_read)
        book = sync_one_book(session, client, user, hardcover_book_id)
    if book is None:
        raise HardcoverError(
            f"book {hardcover_book_id} not on Hardcover shelf after insert"
        )
    return book


def run_sync_for_user(user_id: int) -> dict[str, int]:
    """Run one user's sync (push pending, then pull) with its own session."""
    with get_sessionmaker()() as session:
        user = session.get(User, user_id)
        if user is None:
            raise ValueError(f"user {user_id} not found")
        if not user.hardcover_token:
            user.last_sync_result = "error: no Hardcover token set"
            session.commit()
            return {"created": 0, "updated": 0, "total": 0}
        with HardcoverClient(user.hardcover_token) as client:
            try:
                push_pending(session, client, user)
                return sync_from_hardcover(session, client, user)
            except Exception as exc:
                session.rollback()
                user.last_sync_result = f"error: {exc}"
                session.commit()
                raise


def run_sync_all() -> dict[str, int]:
    """Sync every enabled user that has a token; per-user failures are
    isolated so one bad token can't block the others."""
    with get_sessionmaker()() as session:
        user_ids = session.scalars(
            select(User.id)
            .where(User.enabled, User.hardcover_token != "")
            .order_by(User.id)
        ).all()

    totals = {"created": 0, "updated": 0, "total": 0, "users": 0, "failed": 0}
    for i, user_id in enumerate(user_ids):
        if i:
            time.sleep(USER_SYNC_PAUSE_SECONDS)
        try:
            result = run_sync_for_user(user_id)
        except Exception:
            logger.exception("Hardcover sync failed for user %s", user_id)
            totals["failed"] += 1
            continue
        totals["users"] += 1
        for key in ("created", "updated", "total"):
            totals[key] += result[key]
    return totals


async def hardcover_sync_loop() -> None:
    """Background task: sync all users on startup, then periodically."""
    settings = get_settings()
    while True:
        try:
            await asyncio.to_thread(run_sync_all)
        except Exception:
            logger.exception("Hardcover sync pass failed")
        await asyncio.sleep(settings.sync_interval_minutes * 60)
