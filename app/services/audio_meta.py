"""Scan imported editions' audio files with mutagen for the ABS API:
track order, durations, mime types, and (m4b/mp3) chapters."""

import asyncio
import json
import logging
import re
from pathlib import Path

import mutagen
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.models import AudioFile, Edition
from app.services.importer import AUDIO_EXTS

logger = logging.getLogger(__name__)

MIME_TYPES = {
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
}


def _natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def _read_chapters(parsed) -> list[dict] | None:
    """Best effort: ID3 CHAP frames (mp3). Returns ABS chapter shape or None."""
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
    # MP4 chapter support depends on the mutagen version/file; use if present.
    mp4_chapters = getattr(parsed, "chapters", None)
    if mp4_chapters:
        try:
            return [
                {
                    "id": i,
                    "start": float(ch.start),
                    "end": float(mp4_chapters[i + 1].start) if i + 1 < len(mp4_chapters) else None,
                    "title": ch.title or f"Chapter {i + 1}",
                }
                for i, ch in enumerate(mp4_chapters)
            ]
        except Exception:
            return None
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
        try:
            parsed = mutagen.File(path)
            if parsed is not None and parsed.info is not None:
                duration = float(parsed.info.length)
            chapters = _read_chapters(parsed) if parsed is not None else None
        except Exception:
            logger.exception("Audio scan: failed to parse %s", path)
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
                mime_type=MIME_TYPES.get(path.suffix.lower(), "audio/mpeg"),
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


async def audio_backfill_task() -> None:
    """One-shot startup task: scan any imported editions missing audio metadata."""
    try:
        scanned = await asyncio.to_thread(scan_missing)
        if scanned:
            logger.info("Audio backfill: scanned %d editions", scanned)
    except Exception:
        logger.exception("Audio backfill task failed")
