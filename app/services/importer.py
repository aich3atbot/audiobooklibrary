"""Watch grabbed releases and import finished audiobooks into the
Audiobookshelf-style library layout:

    Author/Series/{index} - Title/   (or Author/Title/ without a series)

We ask the download client (by info hash) how each torrent is doing; it is
authoritative about completion, and its progress is what the Activity page
shows. When it can't answer — a release grabbed before hashes were recorded, a
torrent since removed, a client that is down — we fall back to matching the
release title against the download directory and treating a download as
finished once it has no incomplete-marker files and nothing in it has changed
for download_quiet_seconds.

Failures never guess: the release is flagged for manual review on the Activity
page.
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

from app.clients.download_client import TorrentStatus, get_download_client
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


def cleanup_empty_parents(path: Path, root: Path) -> None:
    """Remove now-empty directories from path up to (excluding) root."""
    current = path
    while current != root and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


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


def poll_download_client(releases: list[Release]) -> dict[str, TorrentStatus] | None:
    """Ask the download client about the torrents we grabbed. Returns None if
    it can't be reached — the caller then falls back to watching the directory,
    so a download client that is down or misconfigured only costs us progress
    reporting, never an import."""
    settings = get_settings()
    hashes = [r.info_hash for r in releases if r.info_hash]
    if not settings.download_url or not hashes:
        return None
    try:
        with get_download_client() as client:
            return client.get_status(hashes)
    except Exception as exc:
        logger.warning("Download client unreachable, falling back to name matching: %s", exc)
        return None


def scan_downloads_once() -> dict[str, int]:
    """One watcher pass. For releases the download client knows by info hash it
    is authoritative — we take its progress and import as soon as it says the
    torrent is finished. Everything else (releases grabbed before hashes were
    recorded, torrents removed from the client, a client that is down) falls
    back to matching the release title against the download directory and
    waiting for the download to go quiet."""
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

        statuses = poll_download_client(list(releases)) or {}
        entries = [p for p in download_dir.iterdir() if not p.name.startswith(".")]
        now = time.time()
        for release in releases:
            status = statuses.get(release.info_hash) if release.info_hash else None
            if status is not None:
                counts["matched"] += 1
                _apply_torrent_status(session, release, status, entries, counts)
                continue

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


def _apply_torrent_status(
    session: Session,
    release: Release,
    status: TorrentStatus,
    entries: list[Path],
    counts: dict[str, int],
) -> None:
    """Record what the download client says, and import if it says finished."""
    release.progress = status.progress
    if not status.is_finished:
        if release.status != "downloading":
            release.status = "downloading"
            release.book.download_state = DownloadState.DOWNLOADING
        session.commit()
        return

    # The client is authoritative about completion, so skip the quiet-period and
    # incomplete-marker heuristics entirely. Find the download by the torrent's
    # own name; its save_path is in the client's filesystem namespace, not ours.
    source = get_settings().download_dir / status.name
    if not source.exists():
        source = next(
            (e for e in entries if matches(status.name, e.name) or matches(release.title, e.name)),
            None,
        )
    if source is None:
        release.status = "failed"
        release.error = (
            f"{status.state}: the download client finished '{status.name}' but it is not in "
            f"{get_settings().download_dir} — check that DOWNLOAD_DIR is the directory it "
            "writes completed downloads to, then retry or import manually."
        )
        release.book.download_state = DownloadState.FAILED
        session.commit()
        counts["failed"] += 1
        logger.warning("Finished torrent %s not found in download dir", status.name)
        return

    if import_release(session, release, source):
        counts["imported"] += 1
    else:
        counts["failed"] += 1


async def download_watch_loop() -> None:
    """Background task: poll the download client and directory for finished
    downloads."""
    settings = get_settings()
    while True:
        try:
            await asyncio.to_thread(scan_downloads_once)
        except Exception:
            logger.exception("Download watcher pass failed")
        await asyncio.sleep(settings.watch_interval_seconds)
