"""Edition bookkeeping: the rows that let one book hold several audiobook
recordings (see plan.md "Multi-edition support"). The label is the grouping
name used in library folder names; "" is the unlabelled edition. Relabelling
an edition moves its library folder to the labelled location."""

import logging
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
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


def relabel_edition(session: Session, edition: Edition, new_label: str) -> Path | None:
    """Change an edition's label and move its library folder to the label's
    location (filesystem first, DB committed only after the move succeeds).
    AudioFile rel_paths are relative to the folder, so they are untouched.
    Returns the new library dir, or None when the edition has no files.
    Raises ImportFailure without committing on any conflict."""
    # Deferred: importer imports downloads, which imports this module.
    from app.services.importer import ImportFailure, cleanup_empty_parents, edition_dir_for

    new_label = new_label.strip()
    for sibling in edition.book.editions:
        if sibling is not edition and sibling.label == new_label:
            shown = new_label or "unlabelled"
            raise ImportFailure(f'the book already has a "{shown}" edition')
    if new_label == edition.label:
        return Path(edition.library_path) if edition.library_path else None

    if not edition.library_path:
        edition.label = new_label
        session.commit()
        return None

    library_dir = get_settings().library_dir
    old_dir = Path(edition.library_path)
    edition.label = new_label  # pending in the session until the move succeeds
    new_dir = edition_dir_for(edition)
    try:
        if new_dir == old_dir:
            session.commit()
            return new_dir
        if new_dir.exists():
            if any(new_dir.iterdir()):
                raise ImportFailure(f"destination already exists: {new_dir}")
            new_dir.rmdir()  # empty leftover; rename must create it
        if old_dir.is_dir():
            if not old_dir.resolve().is_relative_to(library_dir.resolve()):
                raise ImportFailure(f"refusing to move a folder outside the library: {old_dir}")
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                old_dir.rename(new_dir)
            except OSError:
                shutil.move(str(old_dir), str(new_dir))
            cleanup_empty_parents(old_dir.parent, library_dir)
        # A missing old folder is not an error: the path is simply repointed
        # (the files dialog only offers renames for folders that exist).
    except Exception:
        session.rollback()  # un-stage the label change
        raise
    edition.library_path = str(new_dir)
    session.commit()
    logger.info("Relabelled edition of %s -> %r (%s)", edition.book.title, new_label, new_dir)
    return new_dir


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
