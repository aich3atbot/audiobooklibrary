"""Convert an edition that is a pile of MP3s into one chaptered .m4b.

This rewrites files that are already in the library and then deletes the
originals, so everything here is built around proving the new file is good
*before* removing the old ones — see run_job for the exact order.

Chapters come from whatever the edition already has, in the order documented in
plan.md: the chapters the ABS API is already serving (ID3 CHAP frames,
OverDrive markers) win, because then the conversion changes nothing the user
can perceive; a sidecar (.cue, chapters.txt, ffmetadata, ABS metadata.json)
only gets a look when the embedded data is *trivial* — one chapter per file,
which restates the file boundaries the per-file fallback already knows.
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.abs.catalogue import edition_chapters
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import (
    TRANSCODE_ACTIVE,
    Edition,
    Release,
    TranscodeJob,
    TranscodeState,
    User,
    format_series_index,
)
from app.services.audio_format import identify
from app.services.audio_meta import parse_timecode
from app.services.importer import ACTIVE_STATUSES, AUDIO_EXTS, prune_empty_dirs

logger = logging.getLogger(__name__)

# The output is written here first: a leading dot and a non-audio suffix keep
# it invisible to our own scanner and to ABS while it is being written.
TEMP_SUFFIX = ".m4b.part"
CUE_FORMATS = ("MP3", "WAVE", "AIFF", "BINARY", "MOTOROLA", "FLAC", "MP4", "M4A")
CUE_FRAMES_PER_SECOND = 75
# What a chapters.txt line looks like: a timestamp, then the title.
CHAPTERS_TXT_LINE = re.compile(
    r"^\s*[-*]?\s*\[?(\d{1,3}:\d{1,2}(?::\d{1,2})?(?:\.\d{1,3})?)]?\s*[-–—|:\t ]?\s*(.+?)\s*$"
)
# Which files each sidecar pass will look at, by name (case-insensitively —
# the library is full of Chapters.txt as well as chapters.txt). The ffmetadata
# pass casts a wide net over .txt because its own header identifies it.
IS_CUE = lambda name: name.endswith(".cue")  # noqa: E731
IS_CHAPTERS_TXT = lambda name: name == "chapters.txt" or name.endswith(".chapters.txt")  # noqa: E731
IS_FFMETADATA = lambda name: name.endswith((".ffmeta", ".ffmetadata", ".txt"))  # noqa: E731
IS_ABS_METADATA = lambda name: name == "metadata.json"  # noqa: E731

MIN_BITRATE = 16_000
MAX_SAMPLE_RATE = 48_000
# ffmpeg is fed one -i per file; a book with more parts than this gets the
# concat demuxer and a list file instead, to stay well inside ARG_MAX.
MAX_DIRECT_INPUTS = 300
# How far the finished file may sit from what we measured going in. The AAC
# encoder's priming is the only expected difference (21 ms on the reference
# encode) and it does not grow with the book, which is what lets this be a
# flat second rather than a percentage.
DURATION_TOLERANCE = 1.0
# ...plus an allowance for the files that could not be measured and fell back
# to their tag duration, which runs systematically long (see
# measure_durations). It is charged per estimated second, not against the whole
# book: one unmeasurable file in twenty must not be checked as if the other
# nineteen were guesses, and must not have its own drift checked to the second.
LOOSE_TOLERANCE = 0.02
# When to give up on measuring one file. MP3 decodes an order of magnitude
# faster than realtime even on slow hardware, so a quarter of the file's own
# length — never less than five minutes — is generous. A file that reaches it
# is one ffmpeg has hung on, and without a limit that hangs the single worker
# for good: the measure pass is not the loop that watches for a cancel.
MEASURE_TIMEOUT_FLOOR = 300.0
MEASURE_TIMEOUT_RATIO = 0.25
WORKER_POLL_SECONDS = 2.0
# Progress lines between database writes. ffmpeg emits a block twice a second
# and two of its lines carry a position, so 10 is a write every ~2.5s — often
# enough that the Activity page's 5s refresh shows a new number each time,
# rare enough to stay a rounding error next to the encode itself.
PROGRESS_EVERY = 10


class TranscodeFailure(RuntimeError):
    """Anything that stops a transcode. The message reaches the Activity page."""


class TranscodeCancelled(TranscodeFailure):
    """The user asked for it to stop; not a failure to report."""


@dataclass(frozen=True)
class SourceFile:
    """One MP3 going in, as the encoder needs to see it."""

    path: Path
    duration: float
    bitrate: int | None
    channels: int
    sample_rate: int


def measure_durations(
    sources: list[SourceFile],
    ffmpeg: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[float], float]:
    """Each input's *decoded* length, straight from ffmpeg, and how many of
    those seconds are tag estimates rather than measurements.

    Tag-derived durations are not good enough to place chapters. Measured on a
    mixed test set, mutagen reports every MP3 about 0.8% long — the frame count
    includes the encoder delay and padding that a gapless decoder trims. While
    each file is its own track that is invisible (playback starts at the file,
    wherever the tag thinks it ends), but concatenated into one file the error
    accumulates: 0.8% of a ten-hour book is nearly five minutes of chapter
    drift by the end. So the offsets come from a decode pass.

    It costs one extra decode, which is cheap next to what follows — MP3
    decodes an order of magnitude faster than AAC encodes. A file we cannot
    measure keeps its tag duration rather than failing the job (including one
    that takes longer than `MEASURE_TIMEOUT_FLOOR` to decode); the seconds it
    contributes are returned separately so the caller can widen its duration
    check by just that much.

    On a long book this pass is minutes of work before the encode's progress
    bar moves at all, so `should_cancel` is polled between files: without it
    the Stop button does nothing until the encode itself starts."""
    binary = ffmpeg or get_settings().ffmpeg_path
    measured = []
    estimated = 0.0
    for source in sources:
        if should_cancel is not None and should_cancel():
            raise TranscodeCancelled("cancelled by the user")
        seconds = None
        try:
            result = subprocess.run(
                [binary, "-nostdin", "-hide_banner", "-v", "error", "-i", str(source.path),
                 "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
                capture_output=True, text=True, check=False,
                timeout=max(MEASURE_TIMEOUT_FLOOR, source.duration * MEASURE_TIMEOUT_RATIO),
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    value = parse_progress(line)
                    if value is not None:
                        seconds = value
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not measure %s", source.path)
        if seconds is None:
            logger.warning("Falling back to the tag duration for %s", source.path.name)
            seconds = source.duration
            estimated += seconds
        measured.append(seconds)
    return measured, estimated


def probe_sources(paths: list[Path]) -> list[SourceFile]:
    """Header-read every input. Cheap even on a gigabyte book — mutagen never
    touches the payload — and it is what decides the output's bitrate, channel
    layout and sample rate."""
    sources = []
    for path in paths:
        fmt = identify(path)
        if fmt is None:
            raise TranscodeFailure(f"cannot read {path.name} — is it really an MP3?")
        info = fmt.parsed.info
        sources.append(
            SourceFile(
                path=path,
                duration=fmt.duration or 0.0,
                bitrate=getattr(info, "bitrate", None) or None,
                channels=getattr(info, "channels", 2) or 2,
                sample_rate=getattr(info, "sample_rate", 44_100) or 44_100,
            )
        )
    return sources


# --------------------------------------------------------------------------
# Chapters
# --------------------------------------------------------------------------


def _cue_value(rest: str, strip_format: bool = False) -> str:
    """The payload of a cue line: quoted when it has quotes, otherwise the
    whole rest, minus a trailing format token on FILE lines."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else rest[1:]
    if strip_format:
        head, _, tail = rest.rpartition(" ")
        if head and tail.upper() in CUE_FORMATS:
            return head.strip()
    return rest


def _cue_seconds(rest: str) -> float | None:
    """`INDEX 01 MM:SS:FF` — the last field is frames, 75 to the second, and
    the minutes field is not capped at 59."""
    parts = rest.split()
    if len(parts) < 2 or parts[0] not in ("01", "1"):
        return None  # INDEX 00 is the pre-gap, not the chapter start
    fields = parts[1].split(":")
    if len(fields) != 3 or not all(f.isdigit() for f in fields):
        return None
    minutes, seconds, frames = (int(f) for f in fields)
    return minutes * 60 + seconds + frames / CUE_FRAMES_PER_SECOND


def parse_cue(text: str, file_offsets: dict[str, float] | None = None) -> list[dict] | None:
    """Chapters from one cue sheet. See `_cue_sheet` for how it is placed."""
    parsed = _cue_sheet(text, file_offsets)
    return parsed[0] if parsed else None


def _cue_sheet(
    text: str, file_offsets: dict[str, float] | None = None
) -> tuple[list[dict], bool] | None:
    """One cue sheet's chapters, and whether they are *anchored* — placed
    against the edition's own tracks rather than assumed to start at zero.

    Every `FILE` that names a track we know is timed from that track's offset.
    A sheet with several `FILE`s has to place all of them, and is abandoned if
    one does not resolve, because a wrong offset silently scatters every
    chapter after it. A lone `FILE` naming something we cannot place is the
    ordinary whole-book sheet: its times are absolute and it is not anchored.

    That distinction is what lets `_cue_pass` tell a per-disc sheet (anchored,
    one of a set) from a whole-book one (not anchored, on its own)."""
    file_offsets = file_offsets or {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    files = [_cue_value(line[5:], strip_format=True) for line in lines if line[:5].upper() == "FILE "]
    per_file = len(files) > 1

    chapters: list[dict] = []
    offset = 0.0
    anchored = bool(files)
    title = ""
    for line in lines:
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FILE":
            key = Path(_cue_value(rest, strip_format=True)).stem.casefold()
            if key in file_offsets:
                offset = file_offsets[key]
            elif per_file:
                logger.info("Cue sheet names %r, which is not one of the tracks", key)
                return None
            else:
                anchored = False
            title = ""
        elif keyword == "TRACK":
            title = ""  # also drops the sheet's own header TITLE
        elif keyword == "TITLE":
            title = _cue_value(rest)
        elif keyword == "INDEX":
            start = _cue_seconds(rest)
            if start is not None:
                chapters.append({"start": offset + start, "title": title})
    return (chapters, anchored) if chapters else None


def parse_chapters_txt(text: str) -> list[dict] | None:
    """`00:12:33.500 Chapter One` lines, give or take the punctuation people
    put between the two."""
    chapters = []
    for line in text.splitlines():
        match = CHAPTERS_TXT_LINE.match(line)
        if not match:
            continue
        start = parse_timecode(match.group(1))
        title = match.group(2).strip()
        if start is not None and title:
            chapters.append({"start": start, "title": title})
    return chapters or None


def parse_ffmetadata(text: str) -> list[dict] | None:
    """`[CHAPTER]` blocks of an FFMETADATA file, honouring each block's own
    TIMEBASE (ffmpeg writes 1/1000, but 1/1000000000 shows up too)."""
    if not text.lstrip().startswith(";FFMETADATA"):
        return None
    chapters: list[dict] = []
    block: dict[str, str] | None = None

    def flush(block):
        if not block or "START" not in block:
            return
        try:
            start = float(block["START"])
        except ValueError:
            return
        num, _, den = block.get("TIMEBASE", "1/1000").partition("/")
        try:
            scale = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            scale = 1 / 1000
        chapters.append({"start": start * scale, "title": block.get("title", "").strip()})

    for line in text.splitlines():
        line = line.strip()
        if line.upper() == "[CHAPTER]":
            flush(block)
            block = {}
            continue
        if line.startswith("[") or block is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        block[key.strip()] = value.strip()
    flush(block)
    return chapters or None


def parse_abs_metadata(text: str) -> list[dict] | None:
    """The `chapters` array of an Audiobookshelf metadata.json — already our
    own shape, since this app speaks ABS."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(chapters, list):
        return None
    out = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        try:
            start = float(chapter["start"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"start": start, "title": str(chapter.get("title", "")).strip()})
    return out or None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("Could not read %s", path)
        return None


def _sidecars(root: Path, matches) -> list[Path]:
    """Matching files, shallowest first — a sidecar for the whole book sits
    above one that happens to live in a disc folder."""
    found = [p for p in root.rglob("*") if p.is_file() and matches(p.name.casefold())]
    return sorted(found, key=lambda p: (len(p.parts), str(p).casefold()))


def sidecar_is_spent(path: Path) -> bool:
    """Whether a consumed sidecar has nothing left to say once its chapters are
    inside the m4b. A .cue, chapters.txt or ffmetadata file describes only the
    files that are about to be deleted. Audiobookshelf's metadata.json does
    not: it carries description, subtitle, series, narrator and tags alongside
    the chapters, none of which this app can regenerate, so it stays."""
    return not IS_ABS_METADATA(path.name.casefold())


def _cue_pass(root: Path, file_offsets: dict[str, float]) -> tuple[list[dict], list[Path]] | None:
    """The cue sheets, which are the one sidecar that regularly arrives in
    parts: a multi-disc rip ships one per disc, each timed from its own disc's
    zero. Taking only the shallowest would chapter disc one and leave the rest
    of the book bare, with its final chapter stretched over the remainder.

    So: the shallowest sheet that parses leads, and if it is *anchored* to the
    edition's tracks — which is what marks it as describing one part of the
    book — every other anchored sheet is folded in with it. A whole-book sheet
    is not anchored and stands alone, as before; unanchored strays alongside a
    per-disc set are left out rather than laid over it at offset zero."""
    sheets = []
    for path in _sidecars(root, IS_CUE):
        text = _read(path)
        if text is None:
            continue
        try:
            parsed = _cue_sheet(text, file_offsets)
        except Exception:
            logger.exception("Chapter sidecar %s could not be parsed", path)
            continue
        if parsed:
            sheets.append((path, *parsed))
    if not sheets:
        return None

    path, chapters, anchored = sheets[0]
    if not anchored:
        return chapters, [path]
    chapters = list(chapters)
    paths = [path]
    for other, more, other_anchored in sheets[1:]:
        if other_anchored:
            chapters.extend(more)
            paths.append(other)
    chapters.sort(key=lambda c: c["start"])
    return chapters, paths


def sidecar_chapters(
    root: Path, file_offsets: dict[str, float]
) -> tuple[list[dict], list[Path]] | None:
    """The first kind of chapter sidecar in the folder that parses to
    something, with the file (or files — see `_cue_pass`) it came from. The
    caller deletes those once the chapters are inside the m4b, unless one holds
    more than chapters (see `sidecar_is_spent`)."""
    found = _cue_pass(root, file_offsets)
    if found is not None:
        return found
    for matches, parse in (
        (IS_CHAPTERS_TXT, parse_chapters_txt),
        (IS_FFMETADATA, parse_ffmetadata),
        (IS_ABS_METADATA, parse_abs_metadata),
    ):
        for path in _sidecars(root, matches):
            text = _read(path)
            if text is None:
                continue
            try:
                chapters = parse(text)
            except Exception:
                logger.exception("Chapter sidecar %s could not be parsed", path)
                continue
            if chapters:
                return chapters, [path]
    return None


def has_real_embedded_chapters(edition: Edition) -> bool:
    """Whether the files carry chapter data worth more than the file
    boundaries. One chapter per file is not chapter information — it restates
    what the per-file fallback already knows — so those editions let a sidecar
    have a go first."""
    carriers = embedded = 0
    for file in edition.audio_files:
        if not file.chapters_json:
            continue
        try:
            count = len(json.loads(file.chapters_json))
        except ValueError:
            continue
        if count:
            carriers += 1
            embedded += count
    return embedded > carriers


def normalize_chapters(chapters: list[dict], total: float) -> list[dict]:
    """Sort, drop anything past the end, close each chapter on the next one's
    start (the last on the book's end), and number them."""
    cleaned = sorted(
        ({"start": max(0.0, float(c["start"])), "title": (c.get("title") or "").strip()}
         for c in chapters if c.get("start") is not None and float(c["start"]) < total),
        key=lambda c: c["start"],
    )
    out: list[dict] = []
    for chapter in cleaned:
        if out and chapter["start"] <= out[-1]["start"]:
            continue  # a repeat at the same instant would be a zero-length chapter
        out.append(chapter)
    for index, chapter in enumerate(out):
        chapter["id"] = index
        chapter["end"] = out[index + 1]["start"] if index + 1 < len(out) else total
        chapter["title"] = chapter["title"] or f"Chapter {index + 1}"
    return out


def track_offsets(edition: Edition, durations: list[float]) -> dict[str, float]:
    """Where each track starts in the concatenated book, keyed by its bare
    filename — how a sidecar names a file it wants its times placed against.

    A stem that two tracks share (`CD1/Track01.mp3` and `CD2/Track01.mp3`) is
    left out entirely rather than resolved to one of them. Keeping the last
    writer would place a sidecar's chapters on the wrong disc *and* look like a
    successful lookup, so the sheet would be used instead of refused."""
    offsets: dict[str, float] = {}
    ambiguous: set[str] = set()
    running = 0.0
    for index, file in enumerate(edition.audio_files):
        key = Path(file.rel_path).stem.casefold()
        if key in offsets:
            ambiguous.add(key)
        offsets[key] = running
        running += durations[index] if index < len(durations) else (file.duration or 0.0)
    for key in ambiguous:
        logger.info("Two tracks are both named %r; no sidecar can be placed by it", key)
        del offsets[key]
    return offsets


def chapter_plan(
    edition: Edition, durations: list[float]
) -> tuple[list[dict], list[Path]]:
    """The chapters the m4b will carry, and the sidecars they came from (empty
    when they came from the files themselves).

    `durations` are the measured decoded lengths, in track order — every offset
    here is built from them rather than from the stored tag durations."""
    root = Path(edition.library_path)
    total = sum(durations)
    if not has_real_embedded_chapters(edition):
        found = sidecar_chapters(root, track_offsets(edition, durations))
        if found is not None:
            chapters, paths = found
            return normalize_chapters(chapters, total), paths
    # edition_chapters is what the ABS apps are already showing: embedded
    # chapters shifted by each file's offset, else one per track. Reusing it is
    # the point — the conversion must not change the chapter list.
    return normalize_chapters(edition_chapters(edition, durations), total), []


def write_ffmetadata(chapters: list[dict], path: Path) -> None:
    """The chapter file ffmpeg reads. Times in ms, which is plenty: the
    encoder's own padding moves boundaries by more than a millisecond."""
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(chapter['start'] * 1000))}",
            f"END={int(round(chapter['end'] * 1000))}",
            # newlines and '=' would break the parser on the way back in
            f"title={_ffmeta_escape(chapter['title'])}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ffmeta_escape(value: str) -> str:
    for char in ("\\", "=", ";", "#", "\n"):
        value = value.replace(char, "\\" + char if char != "\n" else " ")
    return value


# --------------------------------------------------------------------------
# The encode
# --------------------------------------------------------------------------


def parse_bitrate(value: str) -> int:
    """`64k` / `64000` / `64` -> bits per second."""
    text = str(value).strip().lower().replace("bps", "").strip()
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1]
    try:
        number = float(text)
    except ValueError:
        raise TranscodeFailure(f"TRANSCODE_BITRATE is not a bitrate: {value!r}")
    rate = int(number * multiplier)
    return rate * 1000 if rate < 1000 else rate  # "64" means 64k, not 64bps


def target_bitrate(sources: list[SourceFile], configured: str) -> int:
    """The AAC bitrate to encode at: the configured target, halved for a book
    that is mono throughout, and never above what the source actually holds —
    re-encoding 32k MP3 at 64k spends double the space on no extra audio."""
    rate = parse_bitrate(configured)
    if sources and all(source.channels <= 1 for source in sources):
        rate //= 2
    known = [source.bitrate for source in sources if source.bitrate]
    if known:
        rate = min(rate, max(known))
    return max(rate, MIN_BITRATE)


def output_layout(sources: list[SourceFile]) -> tuple[str, int]:
    """Channel layout and sample rate for the output: stereo if anything in
    the book is stereo, and the best sample rate present (never upsampled past
    48k). Every input is converted to these before concatenation, which is why
    a book of mixed 22/44kHz mono and stereo parts joins cleanly."""
    layout = "mono" if sources and all(s.channels <= 1 for s in sources) else "stereo"
    rate = min(max((s.sample_rate for s in sources), default=44_100), MAX_SAMPLE_RATE)
    return layout, rate


def build_command(
    sources: list[SourceFile],
    meta_path: Path,
    out_path: Path,
    bitrate: int,
    tags: dict[str, str],
    ffmpeg: str = "ffmpeg",
    list_path: Path | None = None,
) -> list[str]:
    """The ffmpeg invocation, verified end to end against mixed-rate inputs.

    Notes that cost real time to rediscover:
    - `-f ipod` is mandatory. The output is a `.part` file, so ffmpeg cannot
      infer the muxer from the extension and refuses to start without it.
    - `-map_metadata`/`-map_chapters` take the *input index* of the metadata
      file, which is last.
    - The concat filter with a per-input `aformat` is deliberate: the concat
      demuxer does not resample between segments, and audiobook MP3s are
      routinely a mix of sample rates and channel counts.
    """
    layout, sample_rate = output_layout(sources)
    command = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error"]

    if list_path is not None:
        command += ["-f", "concat", "-safe", "0", "-i", str(list_path)]
        filters = None
        meta_index = 1
    else:
        for source in sources:
            command += ["-i", str(source.path)]
        parts = [
            f"[{i}:a]aformat=sample_rates={sample_rate}:channel_layouts={layout}[a{i}]"
            for i in range(len(sources))
        ]
        joined = "".join(f"[a{i}]" for i in range(len(sources)))
        parts.append(f"{joined}concat=n={len(sources)}:v=0:a=1[out]")
        filters = ";".join(parts)
        meta_index = len(sources)

    command += ["-i", str(meta_path)]
    if filters is not None:
        command += ["-filter_complex", filters, "-map", "[out]"]
    else:
        command += ["-map", "0:a", "-ar", str(sample_rate), "-ac", "2" if layout == "stereo" else "1"]
    command += ["-map_metadata", str(meta_index), "-map_chapters", str(meta_index)]
    for key, value in tags.items():
        if value:
            command += ["-metadata", f"{key}={value}"]
    command += [
        "-f", "ipod",
        "-c:a", "aac",
        "-b:a", str(bitrate),
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(out_path),
    ]
    return command


def write_concat_list(sources: list[SourceFile], path: Path) -> None:
    """The concat demuxer's list file, for books with more parts than we are
    willing to put on a command line."""
    lines = ["ffconcat version 1.0"]
    for source in sources:
        escaped = str(source.path).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_progress(line: str) -> float | None:
    """Seconds encoded so far, from one `-progress pipe:1` line."""
    key, _, value = line.strip().partition("=")
    try:
        if key == "out_time_us":
            return int(value) / 1_000_000
        if key == "out_time_ms":  # ffmpeg's name is a lie: this is microseconds
            return int(value) / 1_000_000
    except ValueError:
        return None
    return None


def ffmpeg_available(ffmpeg: str | None = None) -> bool:
    """Whether we can transcode at all. The UI hides the button when not."""
    binary = ffmpeg or get_settings().ffmpeg_path
    if shutil.which(binary) is None:
        return False
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def mp3_sources(root: Path) -> list[Path] | None:
    """Every MP3 under the folder, or None when the folder holds any *other*
    audio. Identified by contents, like everything else here — a `.mp3` that
    is really an AAC stream is not something we can concatenate as one, and a
    stray .m4b means the edition is not the pile of MP3s this converts."""
    audio = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    if not audio:
        return None
    mp3s = []
    for path in audio:
        fmt = identify(path)
        if fmt is None or fmt.family != "mp3":
            return None
        mp3s.append(path)
    return mp3s


def active_job(session: Session, edition: Edition) -> TranscodeJob | None:
    return session.scalar(
        select(TranscodeJob)
        .where(TranscodeJob.edition_id == edition.id)
        .where(TranscodeJob.state.in_(TRANSCODE_ACTIVE))
        .order_by(TranscodeJob.id.desc())
    )


def transcode_blocked(session: Session, edition: Edition) -> str | None:
    """Why this edition cannot be converted right now, or None."""
    if not edition.library_path or not Path(edition.library_path).is_dir():
        return "This edition has no files on disk."
    if not ffmpeg_available():
        return "ffmpeg is not available in this container."
    if active_job(session, edition) is not None:
        return "This edition is already being converted."
    downloading = session.scalar(
        select(Release.id)
        .where(Release.edition_id == edition.id)
        .where(Release.status.in_(ACTIVE_STATUSES))
    )
    if downloading is not None:
        # A replace download lands in this same folder.
        return "This edition has a download in flight."
    if mp3_sources(Path(edition.library_path)) is None:
        return "Only editions whose audio is all MP3 can be converted."
    return None


def queue_job(session: Session, edition: Edition, user: User | None) -> TranscodeJob:
    """Add a job for the worker to pick up. Caller has already checked
    transcode_blocked."""
    job = TranscodeJob(
        edition_id=edition.id,
        user_id=user.id if user is not None else None,
        state=TranscodeState.QUEUED,
    )
    session.add(job)
    session.commit()
    logger.info("Queued transcode of %s (edition %s)", edition.book.title, edition.id)
    return job


# --------------------------------------------------------------------------
# Tagging the output
# --------------------------------------------------------------------------


COVER_STEMS = ("cover", "folder")
COVER_EXTS = (".jpg", ".jpeg", ".png")


def output_tags(edition: Edition, source_count: int) -> dict[str, str]:
    """The metadata ffmpeg's MP4 muxer can write. Nothing in this app reads
    these back — the ABS API serves metadata from the database — so they exist
    to make the file a good citizen in any other player."""
    book = edition.book
    tags = {
        "title": book.title,
        "album": book.title,
        "artist": book.author.name,
        "album_artist": book.author.name,
        "composer": edition.narrator,  # the m4b convention for the narrator
        "genre": "Audiobook",
        "media_type": "2",  # marks it an audiobook rather than music
        "gapless_playback": "1",
        "comment": f"Transcoded from {source_count} MP3 files by Audiobook Library",
    }
    if book.hardcover_slug:
        tags["description"] = f"https://hardcover.app/books/{book.hardcover_slug}"
    if book.series is not None:
        tags["grouping"] = book.series.name
        index = format_series_index(book.series_index)
        if index.isdigit():  # -metadata track= wants a number, and 2.5 is not one
            tags["track"] = index
    return tags


def find_cover(root: Path) -> Path | None:
    images = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in COVER_EXTS
    ]
    for stem in COVER_STEMS:
        for image in images:
            if image.stem.casefold() == stem:
                return image
    return images[0] if len(images) == 1 else None


def tag_output(path: Path, edition: Edition, cover: Path | None) -> None:
    """The metadata ffmpeg cannot write: the MP4 "movement" atoms that carry
    series and position, cover art, and a freeform narrator field.

    Verified not to disturb the chapter track, the duration, or the
    moov-before-mdat ordering `+faststart` produced — ffmpeg leaves a `free`
    box that mutagen writes into."""
    from mutagen.mp4 import MP4, MP4Cover

    book = edition.book
    mp4 = MP4(path)
    if mp4.tags is None:
        mp4.add_tags()
    if book.series is not None:
        mp4.tags["\xa9mvn"] = [book.series.name]
        index = format_series_index(book.series_index)
        if index.isdigit():
            mp4.tags["\xa9mvi"] = [int(index)]
        mp4.tags["shwm"] = [1]
    if edition.narrator:
        mp4.tags["----:com.apple.iTunes:NARRATOR"] = [edition.narrator.encode("utf-8")]
    if cover is not None:
        fmt = MP4Cover.FORMAT_PNG if cover.suffix.lower() == ".png" else MP4Cover.FORMAT_JPEG
        try:
            mp4.tags["covr"] = [MP4Cover(cover.read_bytes(), imageformat=fmt)]
        except OSError:
            logger.warning("Could not read cover art %s", cover)
    mp4.save()


# --------------------------------------------------------------------------
# Running a job
# --------------------------------------------------------------------------


def _finish(session: Session, job: TranscodeJob, state: TranscodeState, error: str | None = None):
    job.state = state
    job.error = error
    job.finished_at = datetime.now()
    session.commit()


def _run_encode(session: Session, job: TranscodeJob, command: list[str], total: float) -> float:
    """Run ffmpeg, feeding its progress into the job row and honouring a
    cancel while it runs. Returns the last position ffmpeg reported.

    Cancellation goes through the database rather than a shared process
    handle: the request that wants to stop this sets `cancel_requested`, and
    this loop — which is already reading a progress pipe — notices."""
    logger.info("Transcoding: %s", " ".join(command))
    # stderr goes to a file, never a pipe. We consume stdout line by line for
    # progress and only look at stderr at the end, so a pipe would fill and
    # block ffmpeg mid-encode: at 64 KB, which a book of MP3s with damaged
    # frames reaches easily. One measured case wrote 132 KB of "Header
    # missing" and hung the job at 48% forever.
    stderr_file = tempfile.TemporaryFile(mode="w+", errors="replace")
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=stderr_file, text=True
    )
    position = 0.0
    seen = 0
    try:
        for line in process.stdout:
            value = parse_progress(line)
            if value is None:
                continue
            position = value
            seen += 1
            if seen % PROGRESS_EVERY:
                continue
            job.progress = min(99.0, position / total * 100) if total else None
            session.commit()
            session.refresh(job)  # picks up a cancel written by a request
            if job.cancel_requested:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise TranscodeCancelled("cancelled by the user")
    finally:
        process.stdout.close()
        process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read()
        stderr_file.close()
    if process.returncode != 0:
        tail = " ".join(stderr.split())[-400:]
        raise TranscodeFailure(f"ffmpeg failed ({process.returncode}): {tail or 'no output'}")
    return position


def run_job(session: Session, job: TranscodeJob) -> bool:
    """Convert one edition. Returns True on success.

    The order is the whole point: nothing is deleted until a file has been
    produced, tagged, checked and moved into place, so any failure before the
    rename leaves the edition exactly as it was.
    """
    from app.services.audio_meta import scan_edition_audio

    settings = get_settings()
    edition = job.edition
    root = Path(edition.library_path) if edition.library_path else None
    job.state = TranscodeState.RUNNING
    job.started_at = datetime.now()
    job.progress = 0.0
    session.commit()

    temp = meta_path = list_path = None
    try:
        if root is None or not root.is_dir():
            raise TranscodeFailure("the edition's folder is gone")
        # Re-scan first: the chapter data and the file order both come from
        # audio_file rows, and they have to match what is on disk right now.
        scan_edition_audio(session, edition)
        paths = [root / f.rel_path for f in edition.audio_files]
        missing = [p for p in paths if not p.is_file()]
        if missing:
            raise TranscodeFailure(f"{missing[0].name} disappeared during the scan")
        if mp3_sources(root) is None:
            raise TranscodeFailure("the folder no longer holds only MP3s")

        sources = probe_sources(paths)

        def cancelled() -> bool:
            # Same trick as _run_encode: commit to end this transaction's
            # snapshot, so the refresh can see a cancel another request wrote.
            session.commit()
            session.refresh(job)
            return job.cancel_requested

        durations, estimated = measure_durations(sources, settings.ffmpeg_path, cancelled)
        total = sum(durations)
        if total <= 0:
            raise TranscodeFailure("the files report no duration")

        chapters, sidecars = chapter_plan(edition, durations)
        bitrate = target_bitrate(sources, settings.transcode_bitrate)
        job.source_count = len(sources)
        job.bitrate = bitrate
        session.commit()

        meta_path = root / ".chapters.ffmeta"
        write_ffmetadata(chapters, meta_path)
        temp = root / f".{root.name}{TEMP_SUFFIX}"
        if len(sources) > MAX_DIRECT_INPUTS:
            list_path = root / ".sources.ffconcat"
            write_concat_list(sources, list_path)
        command = build_command(
            sources, meta_path, temp, bitrate, output_tags(edition, len(sources)),
            ffmpeg=settings.ffmpeg_path, list_path=list_path,
        )
        reported = _run_encode(session, job, command, total)

        # Validate before tagging, not after: mutagen cannot open a file that
        # is not really an MP4, and its error is far less useful than ours.
        fmt = identify(temp)
        if fmt is None or fmt.family != "mp4":
            raise TranscodeFailure("the encoded file is not playable audio")
        actual = fmt.duration or 0.0
        # ffmpeg's own last position is the tight check; the measured total is
        # the sanity check on the timeline the chapters were placed against.
        if reported and abs(actual - reported) > DURATION_TOLERANCE:
            raise TranscodeFailure(
                f"the encode stopped at {reported:.0f}s but the file holds {actual:.0f}s"
            )
        # Only the estimated seconds can drift; the measured ones are held to
        # the flat tolerance however long the book is.
        tolerance = DURATION_TOLERANCE + estimated * LOOSE_TOLERANCE
        if abs(actual - total) > tolerance:
            raise TranscodeFailure(
                f"expected about {total:.0f}s of audio but the file holds {actual:.0f}s"
            )

        tag_output(temp, edition, find_cover(root))
        # A tagging pass rewrites the container, so confirm it is still the
        # file we just validated before anything gets deleted for it.
        retagged = identify(temp)
        if retagged is None or abs((retagged.duration or 0.0) - actual) > DURATION_TOLERANCE:
            raise TranscodeFailure("writing the tags damaged the encoded file")

        destination = root / f"{root.name}.m4b"
        if destination.exists():
            raise TranscodeFailure(f"{destination.name} already exists")
        temp.rename(destination)
        temp = None

        for path in paths:
            path.unlink(missing_ok=True)
        for sidecar in sidecars:
            # Their chapters are inside the m4b now, and they describe files
            # that no longer exist.
            if sidecar_is_spent(sidecar):
                sidecar.unlink(missing_ok=True)
        prune_empty_dirs(root)

        job.output_path = str(destination)
        job.progress = 100.0
        scan_edition_audio(session, edition)
        _finish(session, job, TranscodeState.DONE)
        logger.info("Transcoded %s -> %s", edition.book.title, destination)
        return True
    except TranscodeFailure as exc:
        cancelled = isinstance(exc, TranscodeCancelled)
        _finish(
            session, job,
            TranscodeState.CANCELLED if cancelled else TranscodeState.FAILED,
            None if cancelled else str(exc),
        )
        logger.warning("Transcode of %s did not finish: %s", job.edition_id, exc)
        return False
    except Exception as exc:
        logger.exception("Transcode failed for edition %s", job.edition_id)
        _finish(session, job, TranscodeState.FAILED, f"unexpected error: {exc}")
        return False
    finally:
        for path in (temp, meta_path, list_path):
            if path is not None:
                Path(path).unlink(missing_ok=True)


def run_next_job() -> bool:
    """Run the oldest queued job, if there is one. One at a time: ffmpeg will
    take every core it is given, and a queue keeps the box usable."""
    with get_sessionmaker()() as session:
        job = session.scalar(
            select(TranscodeJob)
            .where(TranscodeJob.state == TranscodeState.QUEUED)
            .order_by(TranscodeJob.id)
        )
        if job is None:
            return False
        if job.cancel_requested:
            _finish(session, job, TranscodeState.CANCELLED)
            return True
        run_job(session, job)
        return True


def recover_interrupted_jobs() -> int:
    """Fail anything left running by a restart, and sweep up its temp files.
    The process that owned the encode is gone, so the job cannot be resumed —
    and a half-written .part must not be left to confuse the next attempt."""
    with get_sessionmaker()() as session:
        jobs = session.scalars(
            select(TranscodeJob).where(TranscodeJob.state == TranscodeState.RUNNING)
        ).all()
        for job in jobs:
            library_path = job.edition.library_path if job.edition else None
            if library_path:
                root = Path(library_path)
                for leftover in (root.glob(f"*{TEMP_SUFFIX}"), root.glob(".*.ffmeta"),
                                 root.glob(".*.ffconcat")):
                    for path in leftover:
                        path.unlink(missing_ok=True)
            _finish(session, job, TranscodeState.FAILED, "interrupted by a restart")
        return len(jobs)


async def transcode_worker_loop() -> None:
    """Background task: drain the transcode queue, one job at a time."""
    try:
        recovered = await asyncio.to_thread(recover_interrupted_jobs)
        if recovered:
            logger.info("Failed %d transcode job(s) interrupted by a restart", recovered)
    except Exception:
        logger.exception("Transcode recovery failed")
    while True:
        try:
            if await asyncio.to_thread(run_next_job):
                continue  # something ran; look for the next one straight away
        except Exception:
            logger.exception("Transcode worker pass failed")
        await asyncio.sleep(WORKER_POLL_SECONDS)
