"""Collection import: scan the /imports volume for existing audiobooks,
match them against library books, and move confirmed matches into the
library. Complements (does not touch) the automatic /downloads pipeline.

Unlike download imports, collection imports always MOVE: the point is to
drain /imports into /audiobooks."""

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Book, DownloadState
from app.services.importer import AUDIO_EXTS, ImportFailure, library_dir_for, normalize

logger = logging.getLogger(__name__)

DISC_RE = re.compile(r"^(cd|disc|disk|part)[\s._-]*\d+$", re.IGNORECASE)
HIGH_CONFIDENCE = 0.75
MIN_CONFIDENCE = 0.5


@dataclass
class ImportEntry:
    path: Path  # absolute
    rel: str  # relative to the imports root, stable across rescans
    name: str  # display name (folder name or file stem)
    audio_files: int
    size: int

    @property
    def row_id(self) -> str:
        return hashlib.md5(self.rel.encode()).hexdigest()[:12]


def _direct_audio(path: Path) -> list[Path]:
    return [
        p for p in path.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    ]


def _make_entry(root: Path, path: Path) -> ImportEntry:
    if path.is_file():
        files = [path]
        name = path.stem
    else:
        files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        name = path.name
    return ImportEntry(
        path=path,
        rel=str(path.relative_to(root)),
        name=name,
        audio_files=len(files),
        size=sum(p.stat().st_size for p in files),
    )


def scan_imports() -> list[ImportEntry]:
    """One entry per audiobook. A directory that directly contains audio is a
    leaf entry; a directory whose audio lives only in disc-named children
    (CD1, Disc 2, ...) is grouped as one entry; loose audio files are
    single-file entries."""
    root = get_settings().imports_dir
    entries: list[ImportEntry] = []
    if not root.is_dir():
        return entries

    def walk(directory: Path) -> None:
        children = sorted(
            (c for c in directory.iterdir() if not c.name.startswith(".")),
            key=lambda c: c.name.lower(),
        )
        subdirs = [c for c in children if c.is_dir()]
        for loose in (c for c in children if c.is_file() and c.suffix.lower() in AUDIO_EXTS):
            entries.append(_make_entry(root, loose))
        audio_subdirs = [d for d in subdirs if _direct_audio(d)]
        for d in subdirs:
            if d in audio_subdirs and DISC_RE.match(d.name):
                continue  # handled by the disc-grouping below
            if _direct_audio(d):
                entries.append(_make_entry(root, d))
            else:
                walk(d)
        disc_dirs = [d for d in audio_subdirs if DISC_RE.match(d.name)]
        if disc_dirs and directory != root:
            entries.append(_make_entry(root, directory))

    walk(root)
    return entries


def entry_for(rel: str) -> ImportEntry | None:
    """Rebuild one entry from its relative path, refusing to escape the
    imports root. None if it no longer exists."""
    root = get_settings().imports_dir.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or path == root or not path.exists():
        return None
    return _make_entry(root, path)


def candidate_books(session: Session) -> list[Book]:
    """Books eligible for matching: not already in the library folder."""
    return (
        session.scalars(
            select(Book)
            .where(Book.library_path.is_(None))
            .options(joinedload(Book.author), joinedload(Book.series))
        )
        .unique()
        .all()
    )


def _format_index(index: float) -> str:
    return str(int(index)) if index == int(index) else str(index)


def score_match(entry: ImportEntry, book: Book) -> float:
    """Similarity between an entry (folder name, with parent folders as
    author/series hints) and a book (title / author+title / series forms)."""
    name = normalize(entry.name)
    full = normalize(entry.rel.replace("/", " "))
    targets = [normalize(book.title), normalize(f"{book.author.name} {book.title}")]
    if book.series is not None and book.series_index is not None:
        idx = _format_index(book.series_index)
        targets.append(normalize(f"{idx} {book.title}"))
        targets.append(normalize(f"{book.series.name} {idx} {book.title}"))

    scores = [
        SequenceMatcher(None, cand, target).ratio()
        for cand in (name, full)
        for target in targets
        if target
    ]
    # all title tokens present in the path is a strong signal even when the
    # folder carries extra noise like bitrate tags
    title_tokens = set(normalize(book.title).split())
    if title_tokens and title_tokens <= set(full.split()):
        scores.append(0.9)
    return max(scores, default=0.0)


def best_matches(session: Session, entries: list[ImportEntry]) -> list[dict]:
    """For each entry: {entry, book, score} with book None below threshold."""
    books = candidate_books(session)
    results = []
    for entry in entries:
        best_book, best_score = None, 0.0
        for book in books:
            score = score_match(entry, book)
            if score > best_score:
                best_book, best_score = book, score
        if best_score < MIN_CONFIDENCE:
            best_book = None
        results.append({"entry": entry, "book": best_book, "score": best_score})
    return results


def find_local_matches(session: Session, query: str, limit: int = 5) -> list[Book]:
    like = f"%{query.strip()}%"
    from app.models import Author  # local import to avoid cycles at module load

    return (
        session.scalars(
            select(Book)
            .join(Book.author)
            .where(Book.library_path.is_(None))
            .where(Book.title.ilike(like) | Author.name.ilike(like))
            .options(joinedload(Book.author), joinedload(Book.series))
            .order_by(Book.title)
            .limit(limit)
        )
        .unique()
        .all()
    )


def _cleanup_empty_parents(path: Path, root: Path) -> None:
    current = path
    while current != root and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def import_entry(session: Session, book: Book, entry: ImportEntry) -> Path:
    """Move an entry into the library; raises ImportFailure on any problem."""
    if book.library_path:
        raise ImportFailure(f"{book.title} is already in the library")
    dest = library_dir_for(book)
    if dest.exists() and any(dest.iterdir()):
        raise ImportFailure(f"destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if entry.path.is_file():
        dest.mkdir(exist_ok=True)
        shutil.move(str(entry.path), str(dest / entry.path.name))
    else:
        if dest.exists():
            dest.rmdir()  # empty leftover; shutil.move must create it
        shutil.move(str(entry.path), str(dest))
    _cleanup_empty_parents(entry.path.parent, get_settings().imports_dir)

    book.download_state = DownloadState.IMPORTED
    book.library_path = str(dest)
    session.commit()
    logger.info("Collection import: %s -> %s", entry.rel, dest)

    from app.services.importer import _scan_audio  # deferred: import cycle

    _scan_audio(session, book)
    return dest
