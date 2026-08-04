"""Scan imported editions' audio files with mutagen for the ABS API:
track order, durations, mime types, and (m4b/mp3) chapters."""

import asyncio
import json
import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.models import AppState, AudioFile, Edition
from app.services.audio_format import MIME_TYPES, identify
from app.services.importer import AUDIO_EXTS
from app.services.mp4_chapters import read_mp4_chapters

logger = logging.getLogger(__name__)

MP4_EXTS = (".m4b", ".m4a", ".mp4")
# Bumped when chapter extraction improves, to re-scan libraries scanned by an
# older build (see rescan_for_chapters).
CHAPTER_SCAN_VERSION = "2"
CHAPTER_SCAN_KEY = "audio_chapter_scan_version"


def _natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def _read_chapters(parsed, path: Path) -> list[dict] | None:
    """Best effort: ID3 CHAP frames (mp3), else the MP4 container's own
    chapters (mutagen exposes none — see app/services/mp4_chapters.py).
    Returns the ABS chapter shape or None."""
    chapters = []
    tags = getattr(parsed, "tags", None)
    if tags is not None and hasattr(tags, "getall"):
        chap_frames = tags.getall("CHAP")
        for i, chap in enumerate(sorted(chap_frames, key=lambda c: c.start_time)):
            title = ""
            for sub in chap.sub_frames.values():
                if getattr(sub, "text", None):
                    title = str(sub.text[0])
                    break
            chapters.append(
                {
                    "id": i,
                    "start": chap.start_time / 1000,
                    "end": chap.end_time / 1000,
                    "title": title or f"Chapter {i + 1}",
                }
            )
    if chapters:
        return chapters
    if path.suffix.lower() in MP4_EXTS:
        return read_mp4_chapters(path)
    return None


def scan_edition_audio(session: Session, edition: Edition) -> int:
    """(Re)build audio_file rows for an edition from its library folder.
    Returns the number of tracks found."""
    if not edition.library_path:
        return 0
    root = Path(edition.library_path)
    if not root.is_dir():
        logger.warning(
            "Audio scan: library path missing for %s: %s", edition.book.title, root
        )
        return 0

    paths = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS),
        key=_natural_key,
    )
    edition.audio_files.clear()
    total_end = 0.0
    for i, path in enumerate(paths, start=1):
        duration = None
        chapters = None
        parsed = None
        # Identify by contents, not extension: the importer renames what it
        # can, but a file placed by an older build (or by hand) may still be
        # mislabelled, and mutagen's own sniffing scores on the filename.
        mime = MIME_TYPES.get(path.suffix.lower(), "audio/mpeg")
        try:
            fmt = identify(path)
            if fmt is not None:
                parsed = fmt.parsed
                duration = fmt.duration
                mime = fmt.mime
        except Exception:
            logger.exception("Audio scan: failed to parse %s", path)
        # Chapters are read from the container, so a tag-parse failure must
        # not cost them (and vice versa).
        try:
            chapters = _read_chapters(parsed, path)
        except Exception:
            logger.exception("Audio scan: failed to read chapters from %s", path)
        if chapters:
            # chapter ends may be missing on mp4; close them with track length
            for ch in chapters:
                if ch["end"] is None:
                    ch["end"] = duration or ch["start"]
        stat = path.stat()
        edition.audio_files.append(
            AudioFile(
                index=i,
                rel_path=str(path.relative_to(root)),
                size=stat.st_size,
                mtime_ms=int(stat.st_mtime * 1000),
                duration=duration,
                mime_type=mime,
                chapters_json=json.dumps(chapters) if chapters else None,
            )
        )
        total_end += duration or 0.0
    session.commit()
    logger.info(
        "Audio scan: %s -> %d tracks, %.0fs", edition.book.title, len(paths), total_end
    )
    return len(paths)


def scan_missing() -> int:
    """Backfill: scan imported editions that have no audio_file rows yet."""
    scanned = 0
    with get_sessionmaker()() as session:
        editions = session.scalars(
            select(Edition)
            .where(Edition.library_path.is_not(None))
            .where(~Edition.audio_files.any())
        ).all()
        for edition in editions:
            try:
                if scan_edition_audio(session, edition):
                    scanned += 1
            except Exception:
                logger.exception("Audio backfill failed for %s", edition.book.title)
    return scanned


def rescan_for_chapters() -> int:
    """One-time pass after chapter extraction improves: re-scan editions whose
    MP4 tracks were scanned by an older build and so hold no chapters. Guarded
    by a version marker in app_state, so it runs once, not every startup."""
    with get_sessionmaker()() as session:
        from app.services.sync import get_state, set_state

        if get_state(session, CHAPTER_SCAN_KEY) == CHAPTER_SCAN_VERSION:
            return 0
        editions = session.scalars(
            select(Edition)
            .where(Edition.library_path.is_not(None))
            .where(
                Edition.audio_files.any(
                    AudioFile.chapters_json.is_(None)
                    & AudioFile.mime_type.in_(("audio/mp4",))
                )
            )
        ).all()
        rescanned = 0
        for edition in editions:
            try:
                if scan_edition_audio(session, edition):
                    rescanned += 1
            except Exception:
                logger.exception("Chapter re-scan failed for %s", edition.book.title)
        set_state(session, CHAPTER_SCAN_KEY, CHAPTER_SCAN_VERSION)
        session.commit()
        return rescanned


async def audio_backfill_task() -> None:
    """One-shot startup task: scan any imported editions missing audio metadata,
    then re-scan MP4s whose chapters an older build could not read."""
    try:
        scanned = await asyncio.to_thread(scan_missing)
        if scanned:
            logger.info("Audio backfill: scanned %d editions", scanned)
    except Exception:
        logger.exception("Audio backfill task failed")
    try:
        rescanned = await asyncio.to_thread(rescan_for_chapters)
        if rescanned:
            logger.info("Chapter re-scan: re-scanned %d editions", rescanned)
    except Exception:
        logger.exception("Chapter re-scan task failed")
