"""Watch the download directory for grabbed releases and import finished
audiobooks into the Audiobookshelf-style library layout:

    Author/Series/{index} - Title/   (or Author/Title/ without a series)

Matching is by normalized release title vs. directory/file name. A download
counts as finished when it has no incomplete-marker files and nothing in it
changed for download_quiet_seconds. Failures never guess: the release is
flagged for manual review on the Activity page.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Book, DownloadState, Release

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wma"}
COMPANION_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".nfo", ".cue", ".txt"}
INCOMPLETE_SUFFIXES = (".part", ".!qb", ".crdownload", ".tmp", ".lftp-pget-status")

ACTIVE_STATUSES = ("grabbed", "downloading")


class ImportFailure(RuntimeError):
    pass


def sanitize(name: str) -> str:
    """Make a string safe as a single path component."""
    name = re.sub(r'[\\/:*?"<>|]', " ", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:150] or "Unknown"


def normalize(name: str) -> str:
    """Normalize for release-title vs. filename comparison."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def library_dir_for(book: Book) -> Path:
    settings = get_settings()
    parts = [sanitize(book.author.name)]
    if book.series is not None:
        parts.append(sanitize(book.series.name))
        index = book.series_index
        if index is not None:
            index_str = str(int(index)) if index == int(index) else str(index)
            parts.append(sanitize(f"{index_str} - {book.title}"))
        else:
            parts.append(sanitize(book.title))
    else:
        parts.append(sanitize(book.title))
    return settings.library_dir.joinpath(*parts)


def matches(release_title: str, entry_name: str) -> bool:
    a, b = normalize(release_title), normalize(Path(entry_name).stem)
    if not a or not b:
        return False
    if a == b:
        return True
    # containment either way, guarded so short names can't match everything
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 12 and shorter in longer


def has_incomplete_markers(path: Path) -> bool:
    if path.is_file():
        return path.name.lower().endswith(INCOMPLETE_SUFFIXES)
    return any(
        p.name.lower().endswith(INCOMPLETE_SUFFIXES) for p in path.rglob("*") if p.is_file()
    )


def newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    if path.is_dir():
        for p in path.rglob("*"):
            newest = max(newest, p.stat().st_mtime)
    return newest


def collect_files(source: Path) -> list[tuple[Path, Path]]:
    """(absolute source, relative destination) pairs worth importing."""
    if source.is_file():
        return [(source, Path(source.name))]
    files = []
    for p in sorted(source.rglob("*")):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS | COMPANION_EXTS:
            files.append((p, p.relative_to(source)))
    return files


def _place(src: Path, dest: Path, mode: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "move":
        shutil.move(src, dest)
        return
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def import_release(session: Session, release: Release, source: Path) -> bool:
    """Import a finished download; returns True on success. On failure the
    release is marked failed with the reason and the book flagged."""
    book = release.book
    try:
        files = collect_files(source)
        if not any(src.suffix.lower() in AUDIO_EXTS for src, _ in files):
            raise ImportFailure(f"no audio files found in {source.name}")
        dest = library_dir_for(book)
        if dest.exists() and any(dest.iterdir()):
            raise ImportFailure(f"destination already exists: {dest}")
        dest.mkdir(parents=True, exist_ok=True)
        mode = get_settings().import_mode
        for src, rel in files:
            _place(src, dest / rel, mode)
        if mode == "move" and source.is_dir():
            shutil.rmtree(source, ignore_errors=True)
    except Exception as exc:
        logger.exception("Import failed for release %s", release.title)
        release.status = "failed"
        release.error = str(exc)
        book.download_state = DownloadState.FAILED
        session.commit()
        return False

    release.status = "imported"
    release.error = None
    book.download_state = DownloadState.IMPORTED
    book.library_path = str(dest)
    session.commit()
    logger.info("Imported %s -> %s", release.title, dest)
    _scan_audio(session, book)
    return True


def _scan_audio(session: Session, book) -> None:
    """Audio metadata scan (for the ABS API); never fails the import."""
    from app.services.audio_meta import scan_book_audio  # deferred: import cycle

    try:
        scan_book_audio(session, book)
    except Exception:
        logger.exception("Audio metadata scan failed for %s", book.title)


def scan_downloads_once() -> dict[str, int]:
    """One watcher pass: match active releases against download dir entries,
    mark in-progress downloads, import the finished ones."""
    settings = get_settings()
    counts = {"matched": 0, "imported": 0, "failed": 0}
    download_dir = settings.download_dir
    if not download_dir.is_dir():
        return counts

    with get_sessionmaker()() as session:
        releases = (
            session.scalars(
                select(Release)
                .where(Release.status.in_(ACTIVE_STATUSES))
                .options(
                    joinedload(Release.book).joinedload(Book.author),
                    joinedload(Release.book).joinedload(Book.series),
                )
            )
            .unique()
            .all()
        )
        if not releases:
            return counts

        entries = [p for p in download_dir.iterdir() if not p.name.startswith(".")]
        now = time.time()
        for release in releases:
            entry = next((e for e in entries if matches(release.title, e.name)), None)
            if entry is None:
                continue
            counts["matched"] += 1
            still_busy = (
                has_incomplete_markers(entry)
                or now - newest_mtime(entry) < settings.download_quiet_seconds
            )
            if still_busy:
                if release.status != "downloading":
                    release.status = "downloading"
                    release.book.download_state = DownloadState.DOWNLOADING
                    session.commit()
                continue
            if import_release(session, release, entry):
                counts["imported"] += 1
            else:
                counts["failed"] += 1
    return counts


async def download_watch_loop() -> None:
    """Background task: poll the download directory for finished downloads."""
    settings = get_settings()
    while True:
        try:
            await asyncio.to_thread(scan_downloads_once)
        except Exception:
            logger.exception("Download watcher pass failed")
        await asyncio.sleep(settings.watch_interval_seconds)
