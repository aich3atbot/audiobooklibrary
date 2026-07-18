"""Collection import: scan the /imports volume for existing audiobooks,
identify each one by searching Hardcover (author/series/title heuristics
from the folder names), and move confirmed entries into the library.

Imported books are ownerless: no Hardcover shelving happens here. Users who
have the book in their Hardcover library pick it up automatically on their
next sync (per-user sync attaches their shelf entry to the shared Book row);
everyone else sees it as "available" when they search for it.

Unlike download imports, collection imports always MOVE: the point is to
drain /imports into /audiobooks."""

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.models import Book, DownloadState, Edition
from app.services.editions import get_or_create_edition
from app.services.importer import (
    AUDIO_EXTS,
    ImportFailure,
    cleanup_empty_parents,
    edition_dir_for,
    normalize,
)
from app.services.sync import _load_caches, delete_state, get_state, set_state, upsert_book_metadata

logger = logging.getLogger(__name__)

DISC_RE = re.compile(r"^(cd|disc|disk|part)[\s._-]*\d+$", re.IGNORECASE)
HIGH_CONFIDENCE = 0.75
MIN_CONFIDENCE = 0.5
MATCH_CACHE_PREFIX = "imports_match:"
SEARCH_RESULTS = 5


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


def _format_index(index: float) -> str:
    return str(int(index)) if index == int(index) else str(index)


def hardcover_query(entry: ImportEntry) -> str:
    """A search string from the entry name with its parent folders as
    author/series hints, duplicate words collapsed."""
    tokens: list[str] = []
    for part in (*Path(entry.rel).parts[:-1], entry.name):
        for token in normalize(part).split():
            if token not in tokens:
                tokens.append(token)
    return " ".join(tokens)


def score_result(entry: ImportEntry, result: dict[str, Any]) -> float:
    """Similarity between an entry (folder name, with parent folders as
    author/series hints) and a Hardcover search result."""
    name = normalize(entry.name)
    full = normalize(entry.rel.replace("/", " "))
    title = result["title"]
    author = (result.get("authors") or [""])[0]
    targets = [normalize(title), normalize(f"{author} {title}")]
    if result.get("series_name") and result.get("series_position") is not None:
        idx = _format_index(result["series_position"])
        targets.append(normalize(f"{idx} {title}"))
        targets.append(normalize(f"{result['series_name']} {idx} {title}"))

    scores = [
        SequenceMatcher(None, cand, target).ratio()
        for cand in (name, full)
        for target in targets
        if target
    ]
    # all title tokens present in the path is a strong signal even when the
    # folder carries extra noise like bitrate tags
    title_tokens = set(normalize(title).split())
    if title_tokens and title_tokens <= set(full.split()):
        scores.append(0.9)
    return max(scores, default=0.0)


def match_from_result(result: dict[str, Any], score: float | None) -> dict[str, Any]:
    return {
        "hardcover_id": result["hardcover_id"],
        "title": result["title"],
        "author": (result.get("authors") or [""])[0],
        "series_name": result.get("series_name"),
        "series_position": result.get("series_position"),
        "score": score,
    }


def match_from_book(book: Book) -> dict[str, Any]:
    return {
        "hardcover_id": book.hardcover_id,
        "title": book.title,
        "author": book.author.name,
        "series_name": book.series.name if book.series else None,
        "series_position": book.series_index,
        "score": None,  # manual
    }


def identify_entry(client: HardcoverClient, entry: ImportEntry) -> dict[str, Any] | None:
    """Identify an entry by searching Hardcover; best-scoring result, or None
    below the confidence threshold."""
    results = client.search_books(hardcover_query(entry), per_page=SEARCH_RESULTS)
    if not results:
        results = client.search_books(normalize(entry.name), per_page=SEARCH_RESULTS)
    best, best_score = None, 0.0
    for result in results:
        score = score_result(entry, result)
        if score > best_score:
            best, best_score = result, score
    if best is None or best_score < MIN_CONFIDENCE:
        return None
    return match_from_result(best, best_score)


def cached_match(session: Session, entry: ImportEntry) -> dict[str, Any] | None:
    raw = get_state(session, MATCH_CACHE_PREFIX + entry.rel)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def cache_match(session: Session, rel: str, match: dict[str, Any] | None) -> None:
    set_state(session, MATCH_CACHE_PREFIX + rel, json.dumps(match))
    session.commit()


def clear_cached_match(session: Session, rel: str) -> None:
    delete_state(session, MATCH_CACHE_PREFIX + rel)
    session.commit()


def find_local_matches(session: Session, query: str, limit: int = 5) -> list[Book]:
    """Local books matching a search — lets the user match an entry to a book
    someone already tracks. Available books are legitimate targets too: the
    entry then imports as an additional edition."""
    like = f"%{query.strip()}%"
    from app.models import Author  # local import to avoid cycles at module load

    return (
        session.scalars(
            select(Book)
            .join(Book.author)
            .where(Book.title.ilike(like) | Author.name.ilike(like))
            .options(joinedload(Book.author), joinedload(Book.series))
            .order_by(Book.title)
            .limit(limit)
        )
        .unique()
        .all()
    )


def ensure_book(session: Session, client: HardcoverClient, hardcover_id: int) -> Book:
    """The local Book row for a Hardcover book, creating it from Hardcover
    metadata if nobody tracks it yet. No shelf membership is created."""
    book = session.scalar(select(Book).where(Book.hardcover_id == hardcover_id))
    if book is not None:
        return book
    data = client.fetch_book(hardcover_id)
    if data is None:
        raise ImportFailure(f"Hardcover book {hardcover_id} not found")
    book = upsert_book_metadata(session, data, _load_caches(session))
    session.commit()
    return book


def import_entry(
    session: Session,
    book: Book,
    entry: ImportEntry,
    label: str = "",
    hardcover_edition_id: int | None = None,
    narrator: str = "",
) -> Path:
    """Move an entry into the library, as the book's `label` edition (the
    unlabelled edition by default); raises ImportFailure on any problem."""
    label = label.strip()
    by_label = {e.label: e for e in book.editions}
    if not label and any(e.library_path for e in book.editions):
        raise ImportFailure(
            f"{book.title} is already in the library — give these files an "
            "edition label to import them as another edition"
        )
    target = by_label.get(label)
    if target is not None and target.library_path:
        raise ImportFailure(f'{book.title} already has its "{label}" edition in the library')
    if label:
        unlabelled = by_label.get("")
        if unlabelled is not None and unlabelled.library_path:
            raise ImportFailure(
                f"{book.title}'s existing files need a label first (Rename them "
                "in the book's files dialog) so both editions get named folders"
            )
    edition = get_or_create_edition(session, book, label, hardcover_edition_id, narrator)
    dest = edition_dir_for(edition)
    if dest.exists() and any(dest.iterdir()):
        session.rollback()  # discard a just-created edition row
        raise ImportFailure(f"destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if entry.path.is_file():
        dest.mkdir(exist_ok=True)
        shutil.move(str(entry.path), str(dest / entry.path.name))
    else:
        if dest.exists():
            dest.rmdir()  # empty leftover; shutil.move must create it
        shutil.move(str(entry.path), str(dest))
    cleanup_empty_parents(entry.path.parent, get_settings().imports_dir)

    edition.download_state = DownloadState.IMPORTED
    edition.library_path = str(dest)
    session.commit()
    logger.info("Collection import: %s -> %s", entry.rel, dest)

    from app.services.importer import _scan_audio  # deferred: import cycle

    _scan_audio(session, edition)
    return dest
