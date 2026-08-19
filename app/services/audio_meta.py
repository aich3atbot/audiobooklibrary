"""Scan imported editions' audio files with mutagen for the ABS API:
track order, durations, mime types, title tags, and (m4b/mp3) chapters."""

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
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
CHAPTER_SCAN_VERSION = "3"
CHAPTER_SCAN_KEY = "audio_chapter_scan_version"

# OverDrive's own chapter markers, written into a TXXX frame on every file of a
# library-sourced MP3 audiobook — the one embedded chapter format outside ID3's
# own CHAP frames that turns up often enough to matter. Some rips write the
# description in the singular, hence the prefix match.
OVERDRIVE_DESC_PREFIX = "overdrive mediamarker"

# Where a file's own name for itself lives, by tag format.
TITLE_KEYS = ("TIT2", "\xa9nam", "title", "Title")
TITLE_MAX = 300


def _natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def _marker_seconds(text: str) -> float | None:
    """An OverDrive marker time — `H:MM:SS.mmm`, `MM:SS.mmm` or `SS.mmm`."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for value in values:  # sexagesimal, however many components there are
        seconds = seconds * 60 + value
    return seconds


def _overdrive_chapters(tags) -> list[dict] | None:
    """Chapters from OverDrive MediaMarkers. Markers carry a name and a start
    time relative to *this* file; ends are the next marker's start, and the
    last is left open for the caller to close with the track duration."""
    if tags is None or not hasattr(tags, "getall"):
        return None
    marks: list[tuple[float, str]] = []
    for frame in tags.getall("TXXX"):
        if not str(getattr(frame, "desc", "")).lower().startswith(OVERDRIVE_DESC_PREFIX):
            continue
        for value in frame.text:
            try:
                root = ET.fromstring(str(value).strip())
            except ET.ParseError:
                logger.warning("Unparseable OverDrive markers: %.80s", value)
                continue
            for marker in root.iter("Marker"):
                name = (marker.findtext("Name") or "").strip()
                start = _marker_seconds(marker.findtext("Time") or "")
                if name and start is not None:
                    marks.append((start, name))
    if not marks:
        return None
    marks.sort(key=lambda mark: mark[0])
    chapters: list[dict] = []
    for start, title in marks:
        # A chapter split across files repeats its marker at the same instant;
        # keeping both would make a zero-length chapter.
        if chapters and start <= chapters[-1]["start"]:
            continue
        chapters.append({"id": len(chapters), "start": start, "end": None, "title": title})
    for chapter, following in zip(chapters, chapters[1:]):
        chapter["end"] = following["start"]
    return chapters


def _track_title(parsed) -> str | None:
    """The file's own title tag, or None. Kept so a track whose filename says
    nothing but a number can still name its chapter (app/abs/catalogue.py)."""
    tags = getattr(parsed, "tags", None)
    if tags is None:
        return None
    for key in TITLE_KEYS:
        try:
            value = tags.get(key)
        except Exception:  # exotic tag objects; a title is never worth raising
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        text = str(value).strip() if value is not None else ""
        if text:
            return text[:TITLE_MAX]
    return None


def _read_chapters(parsed, path: Path) -> list[dict] | None:
    """Best effort: ID3 CHAP frames (mp3), then OverDrive MediaMarkers (the
    other embedded format MP3 audiobooks actually ship with), else the MP4
    container's own chapters (mutagen exposes none — see
    app/services/mp4_chapters.py). Returns the ABS chapter shape or None."""
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
    overdrive = _overdrive_chapters(tags)
    if overdrive:
        return overdrive
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
        title = None
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
        try:
            title = _track_title(parsed)
        except Exception:
            logger.exception("Audio scan: failed to read the title tag from %s", path)
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
                title=title,
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
    """One-time pass after chapter (or title) extraction improves, guarded by a
    version marker in app_state so it runs once, not every startup.

    Version 3 re-scans *every* imported edition rather than only the MP4s
    version 2 cared about: OverDrive markers apply to MP3s, and the title tag
    is new on every row, so no narrower predicate would find the rows that need
    it. Scanning is header-only, so a full pass is cheap even on a big
    library."""
    with get_sessionmaker()() as session:
        from app.services.sync import get_state, set_state

        if get_state(session, CHAPTER_SCAN_KEY) == CHAPTER_SCAN_VERSION:
            return 0
        editions = session.scalars(
            select(Edition)
            .where(Edition.library_path.is_not(None))
            .where(Edition.audio_files.any())
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
