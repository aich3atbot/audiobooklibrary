"""Listening progress: upsert from ABS client syncs, and the Hardcover
read-state hooks that hang off it.

Progress is per edition; read state stays book-level, so listening to ANY
edition moves the book on the user's Hardcover shelf:

- past the *started* threshold (5 minutes or 5%, whichever is less) → the book
  becomes *currently reading*, dated today, once;
- finished (remaining <= 10s, ABS default, or the client says so) → *read*,
  dated today;
- left in the trailing credits (past 95%, or all but the last 5 minutes,
  whichever comes first) and then abandoned for another book → *read* too, the
  moment progress arrives for a different book."""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Edition, MediaProgress, ReadState, User
from app.services.sync import get_user_book, update_read_state

logger = logging.getLogger(__name__)

MARK_FINISHED_TIME_REMAINING = 10.0  # seconds, ABS library default

# Listened this far in and the book counts as started.
START_READING_SECONDS = 300.0
START_READING_FRACTION = 0.05
# Listened this far in and only the credits are left.
NEAR_FINISH_SECONDS = 300.0
NEAR_FINISH_FRACTION = 0.95


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_position(duration: float) -> float:
    """Listened far enough to count as started: 5 minutes or 5%, whichever is
    less. With no duration reported yet, fall back to the flat 5 minutes."""
    if not duration:
        return START_READING_SECONDS
    return min(START_READING_SECONDS, START_READING_FRACTION * duration)


def near_finish_position(duration: float) -> float:
    """Close enough to the end that the rest is credits: whichever of "all but
    the last 5 minutes" and "95% through" comes first. For a book shorter than
    that 5-minute tail the time rule is meaningless, so the fraction stands
    alone."""
    if duration <= NEAR_FINISH_SECONDS:
        return NEAR_FINISH_FRACTION * duration
    return min(duration - NEAR_FINISH_SECONDS, NEAR_FINISH_FRACTION * duration)


def _set_read_state(db: Session, user: User, book, state: ReadState, **dates) -> None:
    """Push a read-state change, never failing the progress sync that caused
    it (update_read_state leaves the row pending_push for the sync loop)."""
    logger.info(
        "Listening moved %s to %s on %s's Hardcover", book.title, state.value, user.username
    )
    try:
        update_read_state(db, user, book, state, **dates)
    except Exception:
        logger.exception("Failed to set %s to %s after listening", book.title, state.value)


def _sweep_near_finished(db: Session, user: User, current_edition: Edition) -> None:
    """Starting another book abandons whatever was left in the credits: every
    other book this user left past the near-finish mark is now read.

    Other editions of the *same* book don't count as another book — switching
    recordings is still the same story."""
    rows = db.scalars(
        select(MediaProgress).where(
            MediaProgress.user_id == user.id,
            MediaProgress.is_finished.is_(False),
            MediaProgress.edition_id != current_edition.id,
        )
    ).all()
    for row in rows:
        if row.edition.book_id == current_edition.book_id:
            continue
        if not row.duration or row.current_time < near_finish_position(row.duration):
            continue
        row.is_finished = True
        row.finished_at = _utcnow()
        db.commit()

        book = row.edition.book
        user_book = get_user_book(db, user, book)
        if user_book is None or user_book.read_state != ReadState.READ:
            logger.info(
                "%s was left in the credits and %s moved on; marking it read",
                book.title,
                user.username,
            )
            _set_read_state(db, user, book, ReadState.READ, read_at=date.today())


def apply_progress(
    db: Session,
    user: User,
    edition: Edition,
    current_time: float | None = None,
    duration: float | None = None,
    is_finished: bool | None = None,
) -> MediaProgress:
    """Upsert one user's progress for an edition. is_finished=None means derive
    it from the remaining time; an explicit value (PATCH /me/progress) wins."""

    def _get() -> MediaProgress | None:
        return db.scalar(
            select(MediaProgress).where(
                MediaProgress.user_id == user.id, MediaProgress.edition_id == edition.id
            )
        )

    row = _get()
    if row is None:
        row = MediaProgress(user_id=user.id, edition=edition)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # ABS apps fire sync bursts; another request created the row first
            db.rollback()
            row = _get()

    if duration:
        row.duration = float(duration)
    if current_time is not None:
        row.current_time = float(current_time)

    if is_finished is None:
        # Playing a finished book again from well before the end is a genuine
        # re-listen; anything else (a stray currentTime: 0, a rewind into the
        # last chapter) leaves it finished.
        restarted = (
            row.is_finished
            and bool(row.duration)
            and start_position(row.duration) <= row.current_time < near_finish_position(row.duration)
        )
        if restarted:
            row.is_finished = False
            row.finished_at = None

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

    book = edition.book
    user_book = get_user_book(db, user, book)
    if newly_finished:
        if user_book is None or user_book.read_state != ReadState.READ:
            _set_read_state(db, user, book, ReadState.READ, read_at=date.today())
    elif not finished and row.current_time >= start_position(row.duration):
        # Started listening: promote the book once. A book already marked read
        # promotes too — playing it again is a re-listen.
        if user_book is None or user_book.read_state != ReadState.READING:
            _set_read_state(db, user, book, ReadState.READING, started_at=date.today())

    _sweep_near_finished(db, user, edition)
    return row
