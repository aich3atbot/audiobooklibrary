"""Identify an audio file by its *contents*, not its extension.

Torrent releases lie about extensions. The case that forced this module was a
1.2 GB audiobook named `.mp4` whose first bytes are an ADTS AAC sync word — a
raw AAC elementary stream with no MP4 container at all. mutagen cannot help by
itself: its AAC sniffer scores on the *filename* (`.aac`/`.adts`/`.adif`), so
`mutagen.File()` returns None for that file and every downstream consumer loses
the duration and the mime type.

So we try the parsers explicitly, by content, and report the format we actually
found. The importer renames the library copy to match; the audio scanner uses
the parsed object it gets back.

Extensions are grouped into *families* because several of them name the same
container: .m4b, .m4a and .mp4 are all MP4. A rename only fires when the file's
real family differs from the one its extension claims, so a .m4b is never
"corrected" into a .m4a.

MP4 needs one extra check mutagen cannot do. `MP4Info.load` walks the traks
until it hits a `soun` handler and, finding none, silently falls back to the
`mvhd` duration — so a video-only MP4 parses "successfully". We read the track
handlers out of the container ourselves (reusing the box reader written for
chapters) and reject anything carrying video.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.aac import AAC
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from app.services.mp4_chapters import _find_path, _iter_boxes, _read_top_level_box

logger = logging.getLogger(__name__)

VIDEO_HANDLER = b"vide"
AUDIO_HANDLER = b"soun"

# Extensions that name a video container as readily as an audio one, so the
# importer demands positive proof of audio rather than trusting the name.
AMBIGUOUS_EXTS = {".mp4"}

# family -> (canonical extension, mime type). The canonical extension is what a
# mislabelled file is renamed to.
FAMILIES = {
    "mp4": (".m4b", "audio/mp4"),
    "aac": (".aac", "audio/aac"),
    "mp3": (".mp3", "audio/mpeg"),
    "flac": (".flac", "audio/flac"),
    "vorbis": (".ogg", "audio/ogg"),
    "opus": (".opus", "audio/ogg"),
    "asf": (".wma", "audio/x-ms-wma"),
}

# What each extension claims to be. Extensions absent here are companion files.
EXT_FAMILIES = {
    ".m4b": "mp4",
    ".m4a": "mp4",
    ".mp4": "mp4",
    ".aac": "aac",
    ".adts": "aac",
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "vorbis",
    ".opus": "opus",
    ".wma": "asf",
}

MIME_TYPES = {ext: FAMILIES[family][1] for ext, family in EXT_FAMILIES.items()}
# .ogg may hold either codec; the container mime is the same for both.

# Tried in order when mutagen's own filename-based sniffing comes up empty.
# AAC leads because it is the format mutagen structurally cannot guess, and its
# loader is the strictest of the raw-stream parsers.
_PARSERS: list[tuple[str, type]] = [
    ("aac", AAC),
    ("mp4", MP4),
    ("flac", FLAC),
    ("opus", OggOpus),
    ("vorbis", OggVorbis),
    ("asf", ASF),
    ("mp3", MP3),
]

_CLASS_FAMILIES: list[tuple[type, str]] = [
    (MP4, "mp4"),
    (AAC, "aac"),
    (FLAC, "flac"),
    (OggOpus, "opus"),
    (OggVorbis, "vorbis"),
    (ASF, "asf"),
    (MP3, "mp3"),
]


@dataclass(frozen=True)
class AudioFormat:
    """What a file turned out to be."""

    family: str
    ext: str  # canonical extension for the family
    mime: str
    parsed: mutagen.FileType

    @property
    def duration(self) -> float | None:
        info = getattr(self.parsed, "info", None)
        length = getattr(info, "length", None)
        return float(length) if length is not None else None


def mp4_track_handlers(path: Path) -> set[bytes]:
    """Handler types (`soun`, `vide`, `text`, ...) of the file's tracks, or an
    empty set when it is not an MP4 or the container cannot be read."""
    try:
        with open(path, "rb") as fh:
            moov = _read_top_level_box(fh, b"moov")
        if moov is None:
            return set()
        handlers = set()
        for box_type, start, end in _iter_boxes(moov, 0, len(moov)):
            if box_type != b"trak":
                continue
            hdlr = _find_path(moov, start, end, b"mdia", b"hdlr")
            if hdlr is not None:
                handlers.add(moov[hdlr[0] + 8 : hdlr[0] + 12])
        return handlers
    except Exception:
        logger.exception("MP4 track scan failed for %s", path)
        return set()


def _family_of(parsed: mutagen.FileType) -> str | None:
    for cls, family in _CLASS_FAMILIES:
        if isinstance(parsed, cls):
            return family
    return None


def has_video_track(path: Path) -> bool:
    """True only when the file is positively a video: an MP4 container holding
    a `vide` track. mutagen cannot answer this — `MP4Info.load` walks the traks
    for a `soun` handler and, finding none, silently falls back to the mvhd
    duration, so a video-only MP4 parses as if it were audio.

    Anything that is not an MP4 container yields no handlers and so is never
    called video; this is a positive test, not a soundness check."""
    return VIDEO_HANDLER in mp4_track_handlers(path)


def identify(path: Path) -> AudioFormat | None:
    """The file's real audio format, or None when nothing parses it. Says
    nothing about video — see has_video_track. Header-only: mutagen never
    reads the payload, so this is cheap even on a multi-gigabyte audiobook."""
    parsed = None
    try:
        parsed = mutagen.File(path)
    except Exception:
        logger.debug("mutagen could not open %s", path, exc_info=True)
    family = _family_of(parsed) if parsed is not None else None

    if family is None:
        # mutagen's sniffing scores on the filename, so a mislabelled file
        # lands here. Try the loaders directly against the bytes.
        for candidate, cls in _PARSERS:
            try:
                parsed = cls(path)
            except Exception:
                continue
            family = candidate
            break

    if family is None or parsed is None:
        return None
    ext, mime = FAMILIES[family]
    return AudioFormat(family=family, ext=ext, mime=mime, parsed=parsed)


def corrected_name(path: Path, fmt: AudioFormat) -> str:
    """The filename this file should carry. Unchanged when the extension
    already names the right family — .m4b and .mp4 are both MP4, so neither is
    rewritten into the other."""
    if EXT_FAMILIES.get(path.suffix.lower()) == fmt.family:
        return path.name
    return path.stem + fmt.ext
