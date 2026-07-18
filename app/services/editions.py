"""Edition bookkeeping: the rows that let one book hold several audiobook
recordings (see plan.md "Multi-edition support"). The label is the grouping
name used in library folder names; "" is the unlabelled edition."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Book, Edition

logger = logging.getLogger(__name__)


def get_or_create_edition(
    session: Session,
    book: Book,
    label: str = "",
    hardcover_edition_id: int | None = None,
    narrator: str = "",
) -> Edition:
    """The book's edition with this label, created if absent. Backfills
    Hardcover identity onto an existing row when it was label-only. Caller
    commits."""
    for edition in book.editions:
        if edition.label == label:
            if hardcover_edition_id is not None and edition.hardcover_edition_id is None:
                edition.hardcover_edition_id = hardcover_edition_id
            if narrator and not edition.narrator:
                edition.narrator = narrator
            return edition
    edition = Edition(
        book=book,
        label=label,
        hardcover_edition_id=hardcover_edition_id,
        narrator=narrator,
    )
    session.add(edition)
    return edition


def suggest_labels(session: Session, book: Book) -> list[str]:
    """Labels already in use across the book's series, so edition groups line
    up ("Stephen Fry" for book 3 once books 1-2 use it). Standalone books
    suggest nothing beyond their own labels."""
    query = select(Edition.label).join(Book).where(Edition.label != "").distinct()
    if book.series_id is not None:
        query = query.where(Book.series_id == book.series_id)
    else:
        query = query.where(Book.id == book.id)
    return sorted(session.scalars(query))
