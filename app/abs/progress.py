"""Listening progress: upsert from ABS client syncs, with the finished rule
(remaining time <= 10s, ABS default) and the Hardcover read-state hook."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Book, MediaProgress, ReadState
from app.services.sync import update_read_state

logger = logging.getLogger(__name__)

MARK_FINISHED_TIME_REMAINING = 10.0  # seconds, ABS library default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def apply_progress(
    db: Session,
    book: Book,
    current_time: float | None = None,
    duration: float | None = None,
    is_finished: bool | None = None,
) -> MediaProgress:
    """Upsert a book's progress. is_finished=None means derive it from the
    remaining time; an explicit value (PATCH /me/progress) wins."""
    row = book.media_progress
    if row is None:
        row = MediaProgress(book=book)
        db.add(row)

    if duration:
        row.duration = float(duration)
    if current_time is not None:
        row.current_time = float(current_time)

    if is_finished is None:
        remaining = (row.duration or 0.0) - row.current_time
        finished = bool(row.duration) and remaining <= MARK_FINISHED_TIME_REMAINING
        # never auto-unfinish a finished book from a stale sync
        finished = finished or row.is_finished
    else:
        finished = is_finished

    newly_finished = finished and not row.is_finished
    if newly_finished:
        row.finished_at = _utcnow()
    if not finished:
        row.finished_at = None
    row.is_finished = finished
    db.commit()

    if newly_finished and book.read_state != ReadState.READ:
        logger.info("ABS client finished %s; marking read on Hardcover", book.title)
        try:
            update_read_state(db, book, ReadState.READ)
        except Exception:
            logger.exception("Failed to mark %s read after finishing", book.title)
    return row
