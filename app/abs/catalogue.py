"""Catalogue payload builders for the ABS API: library items, metadata,
shelves, and media progress in the "old model" shapes clients consume.
Shapes are pinned in docs/abs-api-contract.md."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.abs import payloads
from app.config import get_settings
from app.models import Book, Bookmark, MediaProgress, User

COVER_NAMES = ("cover", "folder")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def eligible_books(db: Session) -> list[Book]:
    """Books the API exposes: imported into the library folder."""
    return (
        db.scalars(
            select(Book)
            .where(Book.library_path.is_not(None))
            .options(
                joinedload(Book.author),
                joinedload(Book.series),
                joinedload(Book.audio_files),
                joinedload(Book.media_progress),
            )
        )
        .unique()
        .all()
    )


def get_book_by_item_id(db: Session, item_id: str) -> Book | None:
    book_id = payloads.book_id_from_item(item_id)
    if book_id is None:
        return None
    book = db.get(Book, book_id)
    if book is None or not book.library_path:
        return None
    return book


def title_prefix_at_end(title: str) -> str:
    for prefix in ("The ", "A "):
        if title.startswith(prefix) and len(title) > len(prefix):
            return f"{title[len(prefix):]}, {prefix.strip()}"
    return title


def name_last_first(name: str) -> str:
    parts = name.rsplit(" ", 1)
    return f"{parts[1]}, {parts[0]}" if len(parts) == 2 else name


def _sequence(book: Book) -> str | None:
    if book.series_index is None:
        return None
    index = book.series_index
    return str(int(index)) if index == int(index) else str(index)


def series_name(book: Book) -> str:
    if book.series is None:
        return ""
    seq = _sequence(book)
    return f"{book.series.name} #{seq}" if seq else book.series.name


def find_cover_file(book: Book) -> Path | None:
    if not book.library_path:
        return None
    root = Path(book.library_path)
    if not root.is_dir():
        return None
    images = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    for image in images:
        if image.stem.lower() in COVER_NAMES:
            return image
    return images[0] if images else None


def has_cover(book: Book) -> bool:
    return bool(book.cover_url) or find_cover_file(book) is not None


def book_duration(book: Book) -> float:
    return sum(f.duration or 0.0 for f in book.audio_files)


def book_size(book: Book) -> int:
    return sum(f.size for f in book.audio_files)


def book_chapters(book: Book) -> list[dict[str, Any]]:
    """Embedded chapters when a single file carries them, else one chapter
    per track (matches how ABS treats multi-file books without metadata)."""
    if len(book.audio_files) == 1 and book.audio_files[0].chapters_json:
        return json.loads(book.audio_files[0].chapters_json)
    chapters = []
    offset = 0.0
    for i, file in enumerate(book.audio_files):
        duration = file.duration or 0.0
        chapters.append(
            {
                "id": i,
                "start": offset,
                "end": offset + duration,
                "title": Path(file.rel_path).stem,
            }
        )
        offset += duration
    return chapters


def metadata_minified(book: Book) -> dict[str, Any]:
    return {
        "title": book.title,
        "titleIgnorePrefix": title_prefix_at_end(book.title),
        "subtitle": None,
        "authorName": book.author.name,
        "authorNameLF": name_last_first(book.author.name),
        "narratorName": "",
        "seriesName": series_name(book),
        "genres": [],
        "publishedYear": None,
        "publishedDate": None,
        "publisher": None,
        "description": None,
        "isbn": None,
        "asin": None,
        "language": None,
        "explicit": False,
        "abridged": False,
    }


def metadata_expanded(book: Book) -> dict[str, Any]:
    meta = metadata_minified(book)
    meta["authors"] = [{"id": f"aut_{book.author_id}", "name": book.author.name}]
    meta["narrators"] = []
    meta["series"] = (
        [{"id": f"ser_{book.series_id}", "name": book.series.name, "sequence": _sequence(book)}]
        if book.series
        else []
    )
    meta["descriptionPlain"] = None
    return meta


def audio_file_json(book: Book, file) -> dict[str, Any]:
    root = Path(book.library_path)
    path = root / file.rel_path
    return {
        "index": file.index,
        "ino": str(file.id),
        "metadata": {
            "filename": path.name,
            "ext": path.suffix,
            "path": str(path),
            "relPath": file.rel_path,
            "size": file.size,
            "mtimeMs": file.mtime_ms,
            "ctimeMs": file.mtime_ms,
            "birthtimeMs": file.mtime_ms,
        },
        "addedAt": _ms(book.updated_at),
        "updatedAt": _ms(book.updated_at),
        "trackNumFromMeta": file.index,
        "discNumFromMeta": None,
        "trackNumFromFilename": None,
        "discNumFromFilename": None,
        "format": None,
        "duration": file.duration,
        "bitRate": None,
        "language": None,
        "codec": None,
        "timeBase": None,
        "channels": None,
        "channelLayout": None,
        "chapters": json.loads(file.chapters_json) if file.chapters_json else [],
        "embeddedCoverArt": None,
        "metaTags": None,
        "mimeType": file.mime_type,
    }


def audio_tracks(book: Book) -> list[dict[str, Any]]:
    tracks = []
    offset = 0.0
    item = payloads.item_id(book.id)
    for file in book.audio_files:
        file_json = audio_file_json(book, file)
        tracks.append(
            {
                "index": file.index,
                "startOffset": offset,
                "duration": file.duration,
                "title": file_json["metadata"]["filename"],
                "contentUrl": f"/api/items/{item}/file/{file.id}",
                "mimeType": file.mime_type,
                "codec": None,
                "metadata": file_json["metadata"],
            }
        )
        offset += file.duration or 0.0
    return tracks


def media_minified(book: Book) -> dict[str, Any]:
    chapters = book_chapters(book)
    return {
        "id": f"bk_{book.id}",
        "metadata": metadata_minified(book),
        "coverPath": str(find_cover_file(book) or "") or ("internal" if book.cover_url else None),
        "tags": [],
        "numTracks": len(book.audio_files),
        "numAudioFiles": len(book.audio_files),
        "numChapters": len(chapters),
        "duration": book_duration(book),
        "size": book_size(book),
        "ebookFormat": None,
    }


def item_minified(book: Book) -> dict[str, Any]:
    settings = get_settings()
    path = Path(book.library_path)
    try:
        rel = str(path.relative_to(settings.library_dir))
    except ValueError:
        rel = path.name
    added = _ms(book.created_at)
    updated = _ms(book.updated_at)
    return {
        "id": payloads.item_id(book.id),
        "ino": str(book.id),
        "oldLibraryItemId": None,
        "libraryId": payloads.LIBRARY_ID,
        "folderId": payloads.FOLDER_ID,
        "path": str(path),
        "relPath": rel,
        "isFile": False,
        "mtimeMs": updated,
        "ctimeMs": updated,
        "birthtimeMs": added,
        "addedAt": added,
        "updatedAt": updated,
        "isMissing": False,
        "isInvalid": False,
        "mediaType": "book",
        "media": media_minified(book),
        "numFiles": len(book.audio_files),
        "size": book_size(book),
    }


def item_expanded(book: Book) -> dict[str, Any]:
    item = item_minified(book)
    media = item["media"]
    media["libraryItemId"] = item["id"]
    media["metadata"] = metadata_expanded(book)
    media["audioFiles"] = [audio_file_json(book, f) for f in book.audio_files]
    media["chapters"] = book_chapters(book)
    media["ebookFile"] = None
    media["tracks"] = audio_tracks(book)
    item["libraryFiles"] = []
    return item


def library_json() -> dict[str, Any]:
    settings = get_settings()
    now = payloads.now_ms()
    return {
        "id": payloads.LIBRARY_ID,
        "name": "Audiobooks",
        "folders": [
            {
                "id": payloads.FOLDER_ID,
                "fullPath": str(settings.library_dir),
                "libraryId": payloads.LIBRARY_ID,
                "addedAt": payloads.LIBRARY_CREATED_AT_MS,
            }
        ],
        "displayOrder": 1,
        "icon": "audiobookshelf",
        "mediaType": "book",
        "provider": "custom",
        "settings": {
            "coverAspectRatio": 1,
            "disableWatcher": True,
            "skipMatchingMediaWithAsin": False,
            "skipMatchingMediaWithIsbn": False,
            "autoScanCronExpression": None,
            "audiobooksOnly": True,
            "hideSingleBookSeries": False,
            "onlyShowLaterBooksInContinueSeries": False,
            "metadataPrecedence": [],
            "podcastSearchRegion": "us",
            "markAsFinishedPercentComplete": None,
            "markAsFinishedTimeRemaining": 10,
        },
        "lastScan": None,
        "lastScanVersion": None,
        "createdAt": payloads.LIBRARY_CREATED_AT_MS,
        "lastUpdate": now,
    }


def filterdata(db: Session) -> dict[str, Any]:
    books = eligible_books(db)
    authors = {b.author_id: b.author.name for b in books}
    series = {b.series_id: b.series.name for b in books if b.series}
    return {
        "authors": [{"id": f"aut_{i}", "name": n} for i, n in sorted(authors.items())],
        "genres": [],
        "tags": [],
        "series": [{"id": f"ser_{i}", "name": n} for i, n in sorted(series.items())],
        "narrators": [],
        "languages": [],
        "publishers": [],
        "publishedDecades": [],
    }


SORT_KEYS = {
    "media.metadata.title": lambda b: b.title.lower(),
    "media.metadata.authorName": lambda b: b.author.name.lower(),
    "media.metadata.authorNameLF": lambda b: name_last_first(b.author.name).lower(),
    "addedAt": lambda b: b.created_at,
    "updatedAt": lambda b: b.updated_at,
    "size": book_size,
    "media.duration": book_duration,
}


def sorted_books(books: list[Book], sort: str | None, desc: bool) -> list[Book]:
    key = SORT_KEYS.get(sort or "media.metadata.title", SORT_KEYS["media.metadata.title"])
    return sorted(books, key=key, reverse=desc)


def progress_json(progress: MediaProgress, user: User) -> dict[str, Any]:
    duration = progress.duration or 0.0
    return {
        "id": f"prog_{progress.id}",
        "userId": user.uuid,
        "libraryItemId": payloads.item_id(progress.book_id),
        "episodeId": None,
        "mediaItemId": f"bk_{progress.book_id}",
        "mediaItemType": "book",
        "duration": duration,
        "progress": (progress.current_time / duration) if duration else 0.0,
        "currentTime": progress.current_time,
        "isFinished": progress.is_finished,
        "hideFromContinueListening": False,
        "ebookLocation": None,
        "ebookProgress": None,
        "lastUpdate": _ms(progress.updated_at),
        "startedAt": _ms(progress.started_at),
        "finishedAt": _ms(progress.finished_at),
    }


def all_media_progress(db: Session, user: User) -> list[dict[str, Any]]:
    # Progress is still global; per-user filtering lands with the data pivot.
    rows = db.scalars(select(MediaProgress)).all()
    return [progress_json(row, user) for row in rows]


def bookmark_json(bookmark: Bookmark) -> dict[str, Any]:
    return {
        "libraryItemId": payloads.item_id(bookmark.book_id),
        "title": bookmark.title,
        "time": bookmark.time,
        "createdAt": _ms(bookmark.created_at),
    }


def all_bookmarks(db: Session, user: User) -> list[dict[str, Any]]:
    # Bookmarks are still global; per-user filtering lands with the data pivot.
    rows = db.scalars(select(Bookmark)).all()
    return [bookmark_json(row) for row in rows]


def author_entry(book: Book) -> dict[str, Any]:
    return {
        "id": f"aut_{book.author_id}",
        "asin": None,
        "name": book.author.name,
        "lastFirst": name_last_first(book.author.name),
        "description": None,
        "imagePath": None,
        "addedAt": payloads.LIBRARY_CREATED_AT_MS,
        "updatedAt": payloads.LIBRARY_CREATED_AT_MS,
        "numBooks": 0,
        "libraryId": payloads.LIBRARY_ID,
    }


def search_library(db: Session, query: str, limit: int) -> dict[str, Any]:
    """GET /api/libraries/:id/search response: books matching title/author/
    series, plus matched series (with their books) and authors."""
    q = query.strip().lower()
    books = eligible_books(db)

    book_matches = [
        b for b in books
        if q in b.title.lower()
        or q in b.author.name.lower()
        or (b.series and q in b.series.name.lower())
    ][:limit]

    series_matches: dict[int, dict[str, Any]] = {}
    for book in books:
        if book.series and q in book.series.name.lower():
            group = series_matches.setdefault(
                book.series_id,
                {"series": {"id": f"ser_{book.series_id}", "name": book.series.name},
                 "books": []},
            )
            group["books"].append(item_minified(book))

    author_matches: dict[int, dict[str, Any]] = {}
    for book in books:
        if q in book.author.name.lower():
            entry = author_matches.setdefault(book.author_id, author_entry(book))
            entry["numBooks"] += 1

    return {
        "book": [{"libraryItem": item_expanded(b)} for b in book_matches],
        "narrators": [],
        "tags": [],
        "genres": [],
        "series": list(series_matches.values())[:limit],
        "authors": list(author_matches.values())[:limit],
    }


def personalized_shelves(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    books = eligible_books(db)

    in_progress = [
        b for b in books
        if b.media_progress and not b.media_progress.is_finished
        and b.media_progress.current_time > 0
    ]
    in_progress.sort(key=lambda b: b.media_progress.updated_at, reverse=True)

    recent = sorted(books, key=lambda b: b.created_at, reverse=True)

    finished = [b for b in books if b.media_progress and b.media_progress.is_finished]
    finished.sort(
        key=lambda b: b.media_progress.finished_at or b.media_progress.updated_at, reverse=True
    )

    shelves = []
    if in_progress:
        shelves.append(
            {
                "id": "continue-listening",
                "label": "Continue Listening",
                "labelStringKey": "LabelContinueListening",
                "type": "book",
                "entities": [item_minified(b) for b in in_progress[:limit]],
                "total": len(in_progress),
            }
        )
    if recent:
        shelves.append(
            {
                "id": "recently-added",
                "label": "Recently Added",
                "labelStringKey": "LabelRecentlyAdded",
                "type": "book",
                "entities": [item_minified(b) for b in recent[:limit]],
                "total": len(recent),
            }
        )
    if finished:
        shelves.append(
            {
                "id": "listen-again",
                "label": "Listen Again",
                "labelStringKey": "LabelListenAgain",
                "type": "book",
                "entities": [item_minified(b) for b in finished[:limit]],
                "total": len(finished),
            }
        )
    return shelves
