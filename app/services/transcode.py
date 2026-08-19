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

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.abs.catalogue import edition_chapters
from app.config import get_settings
from app.models import Edition
from app.services.audio_format import identify
from app.services.audio_meta import parse_timecode

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


class TranscodeFailure(RuntimeError):
    """Anything that stops a transcode. The message reaches the Activity page."""


@dataclass(frozen=True)
class SourceFile:
    """One MP3 going in, as the encoder needs to see it."""

    path: Path
    duration: float
    bitrate: int | None
    channels: int
    sample_rate: int


def measure_durations(sources: list[SourceFile], ffmpeg: str | None = None) -> list[float]:
    """Each input's *decoded* length, straight from ffmpeg.

    Tag-derived durations are not good enough to place chapters. Measured on a
    mixed test set, mutagen reports every MP3 about 0.8% long — the frame count
    includes the encoder delay and padding that a gapless decoder trims. While
    each file is its own track that is invisible (playback starts at the file,
    wherever the tag thinks it ends), but concatenated into one file the error
    accumulates: 0.8% of a ten-hour book is nearly five minutes of chapter
    drift by the end. So the offsets come from a decode pass.

    It costs one extra decode, which is cheap next to what follows — MP3
    decodes an order of magnitude faster than AAC encodes. A file we cannot
    measure keeps its tag duration rather than failing the job."""
    binary = ffmpeg or get_settings().ffmpeg_path
    measured = []
    for source in sources:
        seconds = None
        try:
            result = subprocess.run(
                [binary, "-nostdin", "-hide_banner", "-v", "error", "-i", str(source.path),
                 "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
                capture_output=True, text=True, check=False,
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
        measured.append(seconds)
    return measured


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
    """Chapters from a cue sheet, in both shapes that turn up in the wild.

    One `FILE` means the times are absolute over the whole book. Several mean
    each `TRACK` is relative to its own file, so every `FILE` has to resolve to
    one of the edition's tracks — if one doesn't, the sheet is abandoned rather
    than guessed at, because a misplaced offset silently scatters every chapter
    after it."""
    file_offsets = file_offsets or {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    files = [_cue_value(line[5:], strip_format=True) for line in lines if line[:5].upper() == "FILE "]
    per_file = len(files) > 1

    chapters: list[dict] = []
    offset = 0.0
    title = ""
    for line in lines:
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FILE":
            if per_file:
                key = Path(_cue_value(rest, strip_format=True)).stem.casefold()
                if key not in file_offsets:
                    logger.info("Cue sheet names %r, which is not one of the tracks", key)
                    return None
                offset = file_offsets[key]
            title = ""
        elif keyword == "TRACK":
            title = ""  # also drops the sheet's own header TITLE
        elif keyword == "TITLE":
            title = _cue_value(rest)
        elif keyword == "INDEX":
            start = _cue_seconds(rest)
            if start is not None:
                chapters.append({"start": offset + start, "title": title})
    return chapters or None


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


def sidecar_chapters(root: Path, file_offsets: dict[str, float]) -> tuple[list[dict], Path] | None:
    """The first chapter sidecar in the folder that parses to something, with
    the file it came from (the caller deletes it once its chapters are inside
    the m4b — it describes files that will no longer exist)."""
    for matches, parse in (
        (IS_CUE, lambda text: parse_cue(text, file_offsets)),
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
                return chapters, path
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


def chapter_plan(
    edition: Edition, durations: list[float]
) -> tuple[list[dict], Path | None]:
    """The chapters the m4b will carry, and the sidecar they came from (None
    when they came from the files themselves).

    `durations` are the measured decoded lengths, in track order — every offset
    here is built from them rather than from the stored tag durations."""
    root = Path(edition.library_path)
    total = sum(durations)
    if not has_real_embedded_chapters(edition):
        offsets: dict[str, float] = {}
        running = 0.0
        for index, file in enumerate(edition.audio_files):
            offsets[Path(file.rel_path).stem.casefold()] = running
            running += durations[index] if index < len(durations) else (file.duration or 0.0)
        found = sidecar_chapters(root, offsets)
        if found is not None:
            chapters, path = found
            return normalize_chapters(chapters, total), path
    # edition_chapters is what the ABS apps are already showing: embedded
    # chapters shifted by each file's offset, else one per track. Reusing it is
    # the point — the conversion must not change the chapter list.
    return normalize_chapters(edition_chapters(edition, durations), total), None


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
