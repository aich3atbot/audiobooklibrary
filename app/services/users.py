"""User deletion with orphan-book review.

Deleting a user reviews the books only they had: each one on disk can be
deleted from disk or left in place ("available", ownerless); metadata-only
orphans are removed automatically. Books other users also have are untouched
— only this user's memberships/progress/bookmarks die (ORM cascades)."""

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, aliased, joinedload

from app.config import get_settings
from app.models import Book, Edition, MediaProgress, Release, User, UserBook
from app.services.importer import ACTIVE_STATUSES, cleanup_empty_parents

logger = logging.getLogger(__name__)


@dataclass
class OrphanReview:
    """Books only this user has, split by what deletion means for them."""

    on_disk: list[Book] = field(default_factory=list)  # per-book choice
    metadata_only: list[Book] = field(default_factory=list)  # deleted with the user
    downloading: list[Book] = field(default_factory=list)  # kept; finishes ownerless
    # book ids where another user has listening progress (warn: delete-from-disk
    # destroys their resume point; progress doesn't require library membership)
    others_progress: set[int] = field(default_factory=set)


@dataclass
class DeleteReport:
    username: str
    deleted_from_disk: list[str] = field(default_factory=list)  # titles
    removed_metadata: int = 0
    left_in_place: int = 0
    errors: list[str] = field(default_factory=list)


def orphan_books(session: Session, user: User) -> list[Book]:
    """Books in this user's library and nobody else's."""
    other_membership = aliased(UserBook)
    others = select(other_membership.id).where(
        other_membership.book_id == Book.id, other_membership.user_id != user.id
    )
    return (
        session.scalars(
            select(Book)
            .join(UserBook, UserBook.book_id == Book.id)
            .where(UserBook.user_id == user.id, ~exists(others))
            .options(joinedload(Book.author), joinedload(Book.series))
            .order_by(Book.title)
        )
        .unique()
        .all()
    )


def _has_active_release(session: Session, book: Book) -> bool:
    return (
        session.scalar(
            select(Release.id)
            .join(Edition, Release.edition_id == Edition.id)
            .where(Edition.book_id == book.id, Release.status.in_(ACTIVE_STATUSES))
        )
        is not None
    )


def review_orphans(session: Session, user: User) -> OrphanReview:
    review = OrphanReview()
    for book in orphan_books(session, user):
        if any(e.library_path for e in book.editions):
            review.on_disk.append(book)
        elif _has_active_release(session, book):
            review.downloading.append(book)
        else:
            review.metadata_only.append(book)
    if review.on_disk:
        ids = [b.id for b in review.on_disk]
        review.others_progress = set(
            session.scalars(
                select(Edition.book_id)
                .join(MediaProgress, MediaProgress.edition_id == Edition.id)
                .where(Edition.book_id.in_(ids), MediaProgress.user_id != user.id)
            )
        )
    return review


def _delete_book_files(book: Book) -> None:
    """Delete every edition's folder; raises on the first failure."""
    library_root = get_settings().library_dir.resolve()
    for edition in book.editions:
        if not edition.library_path:
            continue
        path = Path(edition.library_path).resolve()
        if not path.is_relative_to(library_root) or path == library_root:
            raise ValueError(f"refusing to delete outside the library: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        cleanup_empty_parents(path.parent, library_root)


def delete_user(session: Session, user: User, delete_disk_ids: set[int]) -> DeleteReport:
    """Delete a user. Orphan books in delete_disk_ids lose their files and
    their Book row (including every user's progress on them); other on-disk
    orphans stay available; metadata-only orphans are removed."""
    report = DeleteReport(username=user.username)
    review = review_orphans(session, user)

    for book in review.on_disk:
        if book.id not in delete_disk_ids:
            report.left_in_place += 1
            continue
        try:
            _delete_book_files(book)
        except Exception as exc:
            logger.exception("Could not delete files for %s", book.title)
            report.errors.append(f"{book.title}: {exc}")
            report.left_in_place += 1
            continue
        report.deleted_from_disk.append(book.title)
        session.delete(book)  # cascades editions → audio files, progress, bookmarks, releases

    for book in review.metadata_only:
        session.delete(book)
        report.removed_metadata += 1

    # attribution on the shared Activity page becomes "deleted user"
    session.execute(update(Release).where(Release.user_id == user.id).values(user_id=None))
    session.delete(user)  # cascades UserBook, MediaProgress, Bookmark, AuthSession
    session.commit()
    logger.info(
        "Deleted user %s: %d book(s) removed from disk, %d metadata-only, %d left available",
        report.username,
        len(report.deleted_from_disk),
        report.removed_metadata,
        report.left_in_place,
    )
    return report
