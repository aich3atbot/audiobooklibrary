"""Catalogue payload builders for the ABS API: library items, metadata,
shelves, and media progress in the "old model" shapes clients consume.
Shapes are pinned in docs/abs-api-contract.md.

A library item is one *edition* (a book can hold several recordings); item
ids are li_<edition.id>. Book metadata is read through edition.book."""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.abs import payloads
from app.config import get_settings
from app.models import Author, Book, Bookmark, Edition, MediaProgress, User

COVER_NAMES = ("cover", "folder")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def eligible_editions(db: Session) -> list[Edition]:
    """Editions the API exposes: imported into the library folder."""
    return (
        db.scalars(
            select(Edition)
            .where(Edition.library_path.is_not(None))
            .options(
                joinedload(Edition.book).joinedload(Book.author),
                joinedload(Edition.book).joinedload(Book.series),
                joinedload(Edition.audio_files),
            )
        )
        .unique()
        .all()
    )


def get_edition_by_item_id(db: Session, item_id: str) -> Edition | None:
    edition_id = payloads.edition_id_from_item(item_id)
    if edition_id is None:
        return None
    edition = db.get(Edition, edition_id)
    if edition is None or not edition.library_path:
        return None
    return edition


def get_author_by_id(db: Session, author_id: str) -> Author | None:
    """`aut_<author.id>` -> Author, and only authors with an exposed edition,
    matching what the rest of the API admits exists."""
    if not author_id.startswith("aut_"):
        return None
    try:
        pk = int(author_id[4:])
    except ValueError:
        return None
    author = db.get(Author, pk)
    if author is None:
        return None
    exposed = db.scalar(
        select(Edition.id)
        .join(Edition.book)
        .where(Book.author_id == pk, Edition.library_path.is_not(None))
        .limit(1)
    )
    return author if exposed else None


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


def find_cover_file(edition: Edition) -> Path | None:
    if not edition.library_path:
        return None
    root = Path(edition.library_path)
    if not root.is_dir():
        return None
    images = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    for image in images:
        if image.stem.lower() in COVER_NAMES:
            return image
    return images[0] if images else None


def has_cover(edition: Edition) -> bool:
    return bool(edition.book.cover_url) or find_cover_file(edition) is not None


def edition_duration(edition: Edition) -> float:
    return sum(f.duration or 0.0 for f in edition.audio_files)


def edition_size(edition: Edition) -> int:
    return sum(f.size for f in edition.audio_files)


def edition_chapters(edition: Edition) -> list[dict[str, Any]]:
    """Embedded chapters, shifted by each file's start offset, and one chapter
    per track for files that carry none (matches how ABS treats multi-file
    books without metadata)."""
    chapters: list[dict[str, Any]] = []
    offset = 0.0
    for file in edition.audio_files:
        duration = file.duration or 0.0
        embedded = json.loads(file.chapters_json) if file.chapters_json else None
        if embedded:
            for chapter in embedded:
                end = chapter["end"]
                chapters.append(
                    {
                        "id": len(chapters),
                        "start": offset + chapter["start"],
                        "end": offset + (duration if end is None else end),
                        "title": chapter["title"],
                    }
                )
        else:
            chapters.append(
                {
                    "id": len(chapters),
                    "start": offset,
                    "end": offset + duration,
                    "title": Path(file.rel_path).stem,
                }
            )
        offset += duration
    return chapters


def display_title(edition: Edition) -> str:
    """The item title clients show. When a book has several editions in the
    library, each item carries its label so list views can tell them apart:
    "Chamber of Secrets (Stephen Fry)". Display-only — book.title is
    untouched."""
    book = edition.book
    siblings = [e for e in book.editions if e.library_path]
    tag = edition.label or edition.narrator
    if len(siblings) > 1 and tag:
        return f"{book.title} ({tag})"
    return book.title


def metadata_minified(edition: Edition) -> dict[str, Any]:
    book = edition.book
    title = display_title(edition)
    return {
        "title": title,
        "titleIgnorePrefix": title_prefix_at_end(title),
        "subtitle": None,
        "authorName": book.author.name,
        "authorNameLF": name_last_first(book.author.name),
        "narratorName": edition.narrator or edition.label,
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


def metadata_expanded(edition: Edition) -> dict[str, Any]:
    book = edition.book
    meta = metadata_minified(edition)
    meta["authors"] = [{"id": f"aut_{book.author_id}", "name": book.author.name}]
    narrator = edition.narrator or edition.label
    meta["narrators"] = [narrator] if narrator else []
    meta["series"] = (
        [{"id": f"ser_{book.series_id}", "name": book.series.name, "sequence": _sequence(book)}]
        if book.series
        else []
    )
    meta["descriptionPlain"] = None
    return meta


def audio_file_json(edition: Edition, file) -> dict[str, Any]:
    root = Path(edition.library_path)
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
        "addedAt": _ms(edition.updated_at),
        "updatedAt": _ms(edition.updated_at),
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


def audio_tracks(edition: Edition) -> list[dict[str, Any]]:
    tracks = []
    offset = 0.0
    item = payloads.item_id(edition.id)
    for file in edition.audio_files:
        file_json = audio_file_json(edition, file)
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


def media_minified(edition: Edition) -> dict[str, Any]:
    chapters = edition_chapters(edition)
    return {
        "id": f"bk_{edition.id}",
        "metadata": metadata_minified(edition),
        "coverPath": str(find_cover_file(edition) or "")
        or ("internal" if edition.book.cover_url else None),
        "tags": [],
        "numTracks": len(edition.audio_files),
        "numAudioFiles": len(edition.audio_files),
        "numChapters": len(chapters),
        "duration": edition_duration(edition),
        "size": edition_size(edition),
        "ebookFormat": None,
    }


def media_full(edition: Edition) -> dict[str, Any]:
    """Book.toOldJSON: the full (non-minified) media block — chapters and
    audio files, no track/duration/size counters."""
    return {
        "id": f"bk_{edition.id}",
        "libraryItemId": payloads.item_id(edition.id),
        "metadata": metadata_expanded(edition),
        "coverPath": str(find_cover_file(edition) or "")
        or ("internal" if edition.book.cover_url else None),
        "tags": [],
        "audioFiles": [audio_file_json(edition, f) for f in edition.audio_files],
        "chapters": edition_chapters(edition),
        "ebookFile": None,
    }


def _item_base(edition: Edition) -> dict[str, Any]:
    settings = get_settings()
    path = Path(edition.library_path)
    try:
        rel = str(path.relative_to(settings.library_dir))
    except ValueError:
        rel = path.name
    added = _ms(edition.created_at)
    updated = _ms(edition.updated_at)
    return {
        "id": payloads.item_id(edition.id),
        "ino": str(edition.id),
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
    }


def item_minified(edition: Edition) -> dict[str, Any]:
    """LibraryItem.toOldJSONMinified: the list/shelf shape. Item *detail*
    must never use this — see item_full."""
    return {
        **_item_base(edition),
        "media": media_minified(edition),
        "numFiles": len(edition.audio_files),
        "size": edition_size(edition),
    }


def item_full(edition: Edition) -> dict[str, Any]:
    """LibraryItem.toOldJSON: what GET /api/items/:id returns without
    `expanded=1`. Clients that skip that param (Lissen) still need the audio
    files and chapters, so this is *not* the minified shape."""
    return {
        **_item_base(edition),
        "lastScan": None,
        "scanVersion": None,
        "media": media_full(edition),
        "libraryFiles": [],
    }


def item_expanded(edition: Edition) -> dict[str, Any]:
    """LibraryItem.toOldJSONExpanded: minified plus the full media block and
    the playable tracks."""
    item = item_minified(edition)
    media = item["media"]
    media.update(media_full(edition))  # incl. expanded metadata
    media["tracks"] = audio_tracks(edition)
    item["lastScan"] = None
    item["scanVersion"] = None
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
    editions = eligible_editions(db)
    authors = {e.book.author_id: e.book.author.name for e in editions}
    series = {e.book.series_id: e.book.series.name for e in editions if e.book.series}
    narrators = sorted({n for e in editions if (n := e.narrator or e.label)})
    return {
        "authors": [{"id": f"aut_{i}", "name": n} for i, n in sorted(authors.items())],
        "genres": [],
        "tags": [],
        "series": [{"id": f"ser_{i}", "name": n} for i, n in sorted(series.items())],
        "narrators": narrators,
        "languages": [],
        "publishers": [],
        "publishedDecades": [],
    }


SORT_KEYS = {
    "media.metadata.title": lambda e: e.book.title.lower(),
    "media.metadata.authorName": lambda e: e.book.author.name.lower(),
    "media.metadata.authorNameLF": lambda e: name_last_first(e.book.author.name).lower(),
    "addedAt": lambda e: e.created_at,
    "updatedAt": lambda e: e.updated_at,
    "size": edition_size,
    "media.duration": edition_duration,
    # Only meaningful within one series; ABS ignores it for other filters.
    "sequence": lambda e: (e.book.series_index is None, e.book.series_index or 0.0),
}


def sorted_editions(editions: list[Edition], sort: str | None, desc: bool,
                    filter_by: str | None = None) -> list[Edition]:
    if sort == "sequence" and not (filter_by or "").startswith("series."):
        sort = None  # ABS drops a sequence sort outside a series filter
    key = SORT_KEYS.get(sort or "media.metadata.title", SORT_KEYS["media.metadata.title"])
    return sorted(editions, key=key, reverse=desc)


# ABS filters are "<group>.<base64 value>" (or a bare group like "issues").
# We hold no genres/tags/publishers/languages, so those groups legitimately
# match nothing — same as upstream with an empty column.
FILTER_GROUPS = ("genres", "tags", "series", "authors", "progress", "narrators",
                 "publishers", "publishedDecades", "missing", "languages", "tracks",
                 "ebooks")


def parse_filter(filter_by: str | None) -> tuple[str, str] | None:
    """-> (group, decoded value), or None when there is no filter."""
    if not filter_by:
        return None
    group = next((g for g in FILTER_GROUPS if filter_by.startswith(f"{g}.")), None)
    if group is None:
        return filter_by, ""
    raw = unquote(filter_by[len(group) + 1 :])
    try:
        value = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        value = ""
    return group, value


def _progress_matches(progress: MediaProgress | None, value: str) -> bool:
    finished = bool(progress and progress.is_finished)
    started = bool(progress and progress.current_time > 0)
    if value == "finished":
        return finished
    if value == "not-finished":
        return not finished
    if value == "in-progress":
        return started and not finished
    if value == "not-started":
        return not started and not finished
    return False


def filtered_editions(
    db: Session, editions: list[Edition], filter_by: str | None, user: User | None
) -> list[Edition]:
    parsed = parse_filter(filter_by)
    if parsed is None:
        return editions
    group, value = parsed
    if group == "series":
        return [e for e in editions if f"ser_{e.book.series_id}" == value]
    if group == "authors":
        return [e for e in editions if f"aut_{e.book.author_id}" == value]
    if group == "narrators":
        return [e for e in editions if (e.narrator or e.label) == value]
    if group == "progress" and user is not None:
        by_edition = {
            p.edition_id: p
            for p in db.scalars(
                select(MediaProgress).where(MediaProgress.user_id == user.id)
            )
        }
        return [e for e in editions if _progress_matches(by_edition.get(e.id), value)]
    # A group we hold no data for (genres, tags, issues, ...) matches nothing.
    return []


def progress_json(progress: MediaProgress, user: User) -> dict[str, Any]:
    duration = progress.duration or 0.0
    return {
        "id": f"prog_{progress.id}",
        "userId": user.uuid,
        "libraryItemId": payloads.item_id(progress.edition_id),
        "episodeId": None,
        "mediaItemId": f"bk_{progress.edition_id}",
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


def get_progress(db: Session, user: User, edition_id: int) -> MediaProgress | None:
    return db.scalar(
        select(MediaProgress).where(
            MediaProgress.user_id == user.id, MediaProgress.edition_id == edition_id
        )
    )


def progress_map(db: Session, user: User) -> dict[int, MediaProgress]:
    """The user's progress rows keyed by edition id."""
    rows = db.scalars(select(MediaProgress).where(MediaProgress.user_id == user.id))
    return {row.edition_id: row for row in rows}


def all_media_progress(db: Session, user: User) -> list[dict[str, Any]]:
    rows = db.scalars(select(MediaProgress).where(MediaProgress.user_id == user.id)).all()
    return [progress_json(row, user) for row in rows]


def bookmark_json(bookmark: Bookmark) -> dict[str, Any]:
    return {
        "libraryItemId": payloads.item_id(bookmark.edition_id),
        "title": bookmark.title,
        "time": bookmark.time,
        "createdAt": _ms(bookmark.created_at),
    }


def all_bookmarks(db: Session, user: User) -> list[dict[str, Any]]:
    rows = db.scalars(select(Bookmark).where(Bookmark.user_id == user.id)).all()
    return [bookmark_json(row) for row in rows]


def author_image_path(author) -> str | None:
    """ABS reports a server-side path here and clients only test it for
    null-ness before asking /api/authors/:id/image — same as coverPath, our
    remote images report "internal"."""
    return "internal" if author.image_url else None


def author_entry(book: Book) -> dict[str, Any]:
    return {
        "id": f"aut_{book.author_id}",
        "asin": None,
        "name": book.author.name,
        "lastFirst": name_last_first(book.author.name),
        "description": None,
        "imagePath": author_image_path(book.author),
        "addedAt": payloads.LIBRARY_CREATED_AT_MS,
        "updatedAt": payloads.LIBRARY_CREATED_AT_MS,
        "numBooks": 0,
        "libraryId": payloads.LIBRARY_ID,
    }


def author_json(author) -> dict[str, Any]:
    """Author.toOldJSON — the single-author payload (no numBooks)."""
    return {
        "id": f"aut_{author.id}",
        "asin": None,
        "name": author.name,
        "description": None,
        "imagePath": author_image_path(author),
        "libraryId": payloads.LIBRARY_ID,
        "addedAt": payloads.LIBRARY_CREATED_AT_MS,
        "updatedAt": payloads.LIBRARY_CREATED_AT_MS,
    }


def author_series_groups(editions: list[Edition]) -> list[dict[str, Any]]:
    """`?include=items,series`: the author's items grouped by series, each
    item's metadata.series flattened to the one series it is grouped under."""
    groups: dict[int, dict[str, Any]] = {}
    for edition in editions:
        book = edition.book
        if book.series is None:
            continue
        group = groups.setdefault(
            book.series_id,
            {"id": f"ser_{book.series_id}", "name": book.series.name, "items": []},
        )
        item = item_minified(edition)
        item["media"]["metadata"]["series"] = {
            "id": f"ser_{book.series_id}",
            "name": book.series.name,
            "nameIgnorePrefix": title_prefix_at_end(book.series.name),
            "sequence": _sequence(book),
        }
        group["items"].append(item)
    for group in groups.values():
        group["items"].sort(key=lambda i: float(i["media"]["metadata"]["series"]["sequence"] or 0))
    return list(groups.values())


def search_library(db: Session, query: str, limit: int) -> dict[str, Any]:
    """GET /api/libraries/:id/search response: books matching title/author/
    series, plus matched series (with their books) and authors."""
    q = query.strip().lower()
    editions = eligible_editions(db)

    edition_matches = [
        e for e in editions
        if q in e.book.title.lower()
        or q in e.book.author.name.lower()
        or (e.book.series and q in e.book.series.name.lower())
    ][:limit]

    series_matches: dict[int, dict[str, Any]] = {}
    for edition in editions:
        book = edition.book
        if book.series and q in book.series.name.lower():
            group = series_matches.setdefault(
                book.series_id,
                {"series": {"id": f"ser_{book.series_id}", "name": book.series.name},
                 "books": []},
            )
            group["books"].append(item_minified(edition))

    author_matches: dict[int, dict[str, Any]] = {}
    for edition in editions:
        book = edition.book
        if q in book.author.name.lower():
            entry = author_matches.setdefault(book.author_id, author_entry(book))
            entry["numBooks"] += 1

    return {
        "book": [{"libraryItem": item_expanded(e)} for e in edition_matches],
        "narrators": [],
        "tags": [],
        "genres": [],
        "series": list(series_matches.values())[:limit],
        "authors": list(author_matches.values())[:limit],
    }


def personalized_shelves(db: Session, user: User, limit: int = 10) -> list[dict[str, Any]]:
    editions = eligible_editions(db)
    progress = progress_map(db, user)

    def prog(e: Edition) -> MediaProgress | None:
        return progress.get(e.id)

    in_progress = [
        e for e in editions
        if prog(e) and not prog(e).is_finished and prog(e).current_time > 0
    ]
    in_progress.sort(key=lambda e: prog(e).updated_at, reverse=True)

    recent = sorted(editions, key=lambda e: e.created_at, reverse=True)

    finished = [e for e in editions if prog(e) and prog(e).is_finished]
    finished.sort(
        key=lambda e: prog(e).finished_at or prog(e).updated_at, reverse=True
    )

    shelves = []
    if in_progress:
        shelves.append(
            {
                "id": "continue-listening",
                "label": "Continue Listening",
                "labelStringKey": "LabelContinueListening",
                "type": "book",
                "entities": [item_minified(e) for e in in_progress[:limit]],
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
                "entities": [item_minified(e) for e in recent[:limit]],
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
                "entities": [item_minified(e) for e in finished[:limit]],
                "total": len(finished),
            }
        )
    return shelves
