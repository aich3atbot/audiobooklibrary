"""Listening progress: upsert from ABS client syncs, and the Hardcover
read-state hooks that hang off it.

Progress is per edition; read state stays book-level, so listening to ANY
edition moves the book on the user's Hardcover shelf:

- past the *started* threshold (MARK_READING_AFTER_MINUTES) → the book becomes
  *currently reading*, dated today, once;
- finished (remaining <= 10s, ABS default, or the client says so) → *read*,
  dated today;
- left in the trailing credits (within MARK_READ_TAIL_MINUTES of the end) and
  then abandoned for another book → *read* too, the moment progress arrives for
  a different book."""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Edition, MediaProgress, ReadState, User
from app.services.sync import get_user_book, update_read_state

logger = logging.getLogger(__name__)

MARK_FINISHED_TIME_REMAINING = 10.0  # seconds, ABS library default

# Lower bound for treating a sync as a re-listen rather than a stray report.
# Deliberately not configurable and not tied to MARK_READING_AFTER_MINUTES:
# that can be set to 0, and un-finishing a book on a spurious currentTime: 0
# would drop it back into Continue Listening for no reason.
RELISTEN_MIN_POSITION = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_position() -> float:
    """Listened this long and the book counts as started. 0 promotes on the
    first progress sync after playback begins."""
    return get_settings().mark_reading_after_minutes * 60.0


def near_finish_position(duration: float) -> float:
    """Close enough to the end that only the credits are left.

    The tail never eats more than half the book: subtracting a 30-minute tail
    from a 20-minute book would otherwise mark it read from the first second.
    That clamp is the only place a proportion is involved, and only for books
    shorter than the configured tail."""
    tail = min(get_settings().mark_read_tail_minutes * 60.0, duration / 2)
    return duration - tail


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
    hide_from_continue_listening: bool | None = None,
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
        if row.current_time != float(current_time):
            # Upstream un-hides a book from Continue Listening the moment its
            # position moves (MediaProgress.applyProgressUpdate); an explicit
            # flag in the same request wins and is re-applied by the caller.
            row.hide_from_continue_listening = False
        row.current_time = float(current_time)
    if hide_from_continue_listening is not None:
        row.hide_from_continue_listening = hide_from_continue_listening

    if is_finished is None:
        # Playing a finished book again from well before the end is a genuine
        # re-listen; anything else (a stray currentTime: 0, a rewind into the
        # last chapter) leaves it finished.
        restarted = (
            row.is_finished
            and bool(row.duration)
            and RELISTEN_MIN_POSITION <= row.current_time < near_finish_position(row.duration)
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
    elif not finished and row.current_time >= start_position():
        # Started listening: promote the book once. A book already marked read
        # promotes too — playing it again is a re-listen.
        if user_book is None or user_book.read_state != ReadState.READING:
            _set_read_state(db, user, book, ReadState.READING, started_at=date.today())

    _sweep_near_finished(db, user, edition)
    return row


def _from_ms(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(tzinfo=None)


def set_progress_dates(
    db: Session,
    user: User,
    row: MediaProgress,
    created_at: object = None,
    finished_at: object = None,
) -> None:
    """Honour explicit dates sent with PATCH /api/me/progress/:id.

    ABS shows a book's *start* date as the progress row's `createdAt`, and
    clients edit it by re-sending that field (Absorb's "change start date");
    `finishedAt` edits the finish date the same way. Both are user-visible read
    state, so mirror them onto the book's shelf entry — that is what reaches
    Hardcover. A book the user doesn't have on a shelf keeps the dates locally;
    editing a date is not a reason to add it to their library."""
    started = _from_ms(created_at)
    finished = _from_ms(finished_at)
    if started is None and finished is None:
        return

    if started is not None:
        row.started_at = started
    if finished is not None and row.is_finished:
        row.finished_at = finished
    db.commit()

    book = row.edition.book
    user_book = get_user_book(db, user, book)
    if user_book is None:
        return
    read_at = None
    if finished is not None and user_book.read_state == ReadState.READ:
        read_at = finished.date()
    _set_read_state(
        db,
        user,
        book,
        user_book.read_state,  # a date edit never moves the shelf
        read_at=read_at,
        started_at=started.date() if started else None,
    )
