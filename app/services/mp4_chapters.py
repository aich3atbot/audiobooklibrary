"""Chapter extraction for MP4 audiobooks (.m4b/.m4a/.mp4).

mutagen parses MP4 tags but exposes no chapter data, so we read the container
ourselves. Two layouts are in the wild and both appear in audiobook rips:

* **Nero** — a `moov/udta/chpl` list of (start, title), times in 100ns ticks.
* **QuickTime** — a separate *text track* holding one sample per chapter,
  pointed at by the audio track's `tref/chap`. Titles live in the mdat with
  the samples; start times come from the text track's `stts` deltas.

Nero wins when both are present (it is the cheaper, more reliable read).
Everything is best effort: a malformed box yields None, never an exception.
"""

import logging
from pathlib import Path
from typing import BinaryIO, Iterator

logger = logging.getLogger(__name__)

# Handler types used by chapter text tracks.
TEXT_HANDLERS = (b"text", b"sbtl")
NERO_TICKS_PER_SECOND = 10_000_000


def _iter_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield (type, payload start, payload end) for the boxes in data[start:end]."""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos : pos + 4], "big")
        box_type = data[pos + 4 : pos + 8]
        header = 8
        if size == 1:  # 64-bit extended size
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:  # runs to the end of its parent
            size = end - pos
        if size < header or pos + size > end:
            return
        yield box_type, pos + header, pos + size
        pos += size


def _find_box(data: bytes, start: int, end: int, box_type: bytes) -> tuple[int, int] | None:
    for found, box_start, box_end in _iter_boxes(data, start, end):
        if found == box_type:
            return box_start, box_end
    return None


def _find_path(data: bytes, start: int, end: int, *path: bytes) -> tuple[int, int] | None:
    for box_type in path:
        found = _find_box(data, start, end, box_type)
        if found is None:
            return None
        start, end = found
    return start, end


def _read_top_level_box(fh: BinaryIO, wanted: bytes) -> bytes | None:
    """Read one top-level box's payload without slurping the whole file
    (audiobooks run to gigabytes; `moov` is a few hundred KB)."""
    fh.seek(0, 2)
    file_size = fh.tell()
    pos = 0
    while pos + 8 <= file_size:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[0:4], "big")
        box_type = header[4:8]
        header_len = 8
        if size == 1:
            size = int.from_bytes(fh.read(8), "big")
            header_len = 16
        elif size == 0:
            size = file_size - pos
        if size < header_len:
            return None
        if box_type == wanted:
            fh.seek(pos + header_len)
            return fh.read(size - header_len)
        pos += size
    return None


def _parse_chpl(data: bytes, start: int, end: int) -> list[tuple[float, str]]:
    """Nero chapter list: version, flags, [u32 when version != 0], u8 count,
    then per chapter u64 start (100ns ticks), u8 title length, UTF-8 title."""
    if end - start < 5:
        return []
    version = data[start]
    pos = start + 4
    if version:
        pos += 4
    if pos >= end:
        return []
    count = data[pos]
    pos += 1
    chapters = []
    for _ in range(count):
        if pos + 9 > end:
            break
        ticks = int.from_bytes(data[pos : pos + 8], "big")
        pos += 8
        length = data[pos]
        pos += 1
        title = _decode_title(data[pos : pos + length])
        pos += length
        chapters.append((ticks / NERO_TICKS_PER_SECOND, title))
    return chapters


def _decode_title(raw: bytes) -> str:
    if raw[:2] in (b"\xfe\xff", b"\xff\xfe"):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    return raw.decode(encoding, "replace").strip("\x00").strip()


def _parse_stts(data: bytes, start: int, end: int) -> list[int]:
    """Sample deltas, expanded to one entry per sample."""
    if end - start < 8:
        return []
    count = int.from_bytes(data[start + 4 : start + 8], "big")
    pos = start + 8
    deltas: list[int] = []
    for _ in range(count):
        if pos + 8 > end:
            break
        sample_count = int.from_bytes(data[pos : pos + 4], "big")
        delta = int.from_bytes(data[pos + 4 : pos + 8], "big")
        pos += 8
        if sample_count > 100_000:  # guard against a corrupt table
            return []
        deltas.extend([delta] * sample_count)
    return deltas


def _parse_stsz(data: bytes, start: int, end: int) -> list[int]:
    if end - start < 12:
        return []
    uniform = int.from_bytes(data[start + 4 : start + 8], "big")
    count = int.from_bytes(data[start + 8 : start + 12], "big")
    if uniform:
        return [uniform] * count
    pos = start + 12
    sizes = []
    for _ in range(count):
        if pos + 4 > end:
            break
        sizes.append(int.from_bytes(data[pos : pos + 4], "big"))
        pos += 4
    return sizes


def _parse_stsc(data: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """-> [(first chunk (1-based), samples per chunk)]"""
    if end - start < 8:
        return []
    count = int.from_bytes(data[start + 4 : start + 8], "big")
    pos = start + 8
    entries = []
    for _ in range(count):
        if pos + 12 > end:
            break
        entries.append(
            (
                int.from_bytes(data[pos : pos + 4], "big"),
                int.from_bytes(data[pos + 4 : pos + 8], "big"),
            )
        )
        pos += 12
    return entries


def _parse_chunk_offsets(data: bytes, stbl: tuple[int, int]) -> list[int]:
    start, end = stbl
    box = _find_box(data, start, end, b"stco")
    width = 4
    if box is None:
        box = _find_box(data, start, end, b"co64")
        width = 8
    if box is None:
        return []
    box_start, box_end = box
    count = int.from_bytes(data[box_start + 4 : box_start + 8], "big")
    pos = box_start + 8
    offsets = []
    for _ in range(count):
        if pos + width > box_end:
            break
        offsets.append(int.from_bytes(data[pos : pos + width], "big"))
        pos += width
    return offsets


def _sample_offsets(chunk_offsets: list[int], stsc: list[tuple[int, int]],
                    sizes: list[int]) -> list[int]:
    """Walk the chunk table to a file offset per sample."""
    if not chunk_offsets or not sizes:
        return []
    if not stsc:
        stsc = [(1, 1)]
    offsets: list[int] = []
    sample = 0
    for chunk_index, chunk_offset in enumerate(chunk_offsets, start=1):
        per_chunk = stsc[0][1]
        for first_chunk, samples in stsc:
            if first_chunk <= chunk_index:
                per_chunk = samples
            else:
                break
        position = chunk_offset
        for _ in range(per_chunk):
            if sample >= len(sizes):
                return offsets
            offsets.append(position)
            position += sizes[sample]
            sample += 1
    return offsets


def _chapter_track(data: bytes, moov_end: int) -> tuple[int, int] | None:
    """The trak holding chapter text: the one another track points at with
    `tref/chap`, else any text-handler track."""
    traks = [(s, e) for t, s, e in _iter_boxes(data, 0, moov_end) if t == b"trak"]
    referenced: set[int] = set()
    by_id: dict[int, tuple[int, int]] = {}
    text_traks: list[tuple[int, int]] = []
    for start, end in traks:
        tkhd = _find_box(data, start, end, b"tkhd")
        if tkhd is not None:
            version = data[tkhd[0]]
            track_id_at = tkhd[0] + (20 if version == 1 else 12)
            if track_id_at + 4 <= tkhd[1]:
                by_id[int.from_bytes(data[track_id_at : track_id_at + 4], "big")] = (start, end)
        chap = _find_path(data, start, end, b"tref", b"chap")
        if chap is not None:
            chap_start, chap_end = chap
            for pos in range(chap_start, chap_end - 3, 4):
                referenced.add(int.from_bytes(data[pos : pos + 4], "big"))
        hdlr = _find_path(data, start, end, b"mdia", b"hdlr")
        if hdlr is not None and data[hdlr[0] + 8 : hdlr[0] + 12] in TEXT_HANDLERS:
            text_traks.append((start, end))
    for track_id in referenced:
        trak = by_id.get(track_id)
        if trak is not None and trak in text_traks:
            return trak
    return text_traks[0] if text_traks else None


def _parse_text_track(fh: BinaryIO, data: bytes, trak: tuple[int, int]) -> list[tuple[float, str]]:
    start, end = trak
    mdhd = _find_path(data, start, end, b"mdia", b"mdhd")
    stbl = _find_path(data, start, end, b"mdia", b"minf", b"stbl")
    if mdhd is None or stbl is None:
        return []
    version = data[mdhd[0]]
    timescale_at = mdhd[0] + (20 if version == 1 else 12)
    timescale = int.from_bytes(data[timescale_at : timescale_at + 4], "big")
    if not timescale:
        return []

    stts = _find_box(data, *stbl, b"stts")
    stsz = _find_box(data, *stbl, b"stsz")
    stsc = _find_box(data, *stbl, b"stsc")
    if stts is None or stsz is None:
        return []
    deltas = _parse_stts(data, *stts)
    sizes = _parse_stsz(data, *stsz)
    offsets = _sample_offsets(
        _parse_chunk_offsets(data, stbl),
        _parse_stsc(data, *stsc) if stsc else [],
        sizes,
    )
    if not offsets or not deltas:
        return []

    chapters = []
    elapsed = 0
    for i, offset in enumerate(offsets):
        if i >= len(deltas) or i >= len(sizes):
            break
        fh.seek(offset)
        sample = fh.read(sizes[i])
        title = ""
        if len(sample) >= 2:
            length = int.from_bytes(sample[0:2], "big")
            title = _decode_title(sample[2 : 2 + length])
        chapters.append((elapsed / timescale, title))
        elapsed += deltas[i]
    return chapters


def read_mp4_chapters(path: Path) -> list[dict] | None:
    """Embedded chapters in ABS shape, or None when the file has none."""
    try:
        with open(path, "rb") as fh:
            moov = _read_top_level_box(fh, b"moov")
            if moov is None:
                return None
            raw: list[tuple[float, str]] = []
            chpl = _find_path(moov, 0, len(moov), b"udta", b"chpl")
            if chpl is not None:
                raw = _parse_chpl(moov, *chpl)
            if not raw:
                trak = _chapter_track(moov, len(moov))
                if trak is not None:
                    raw = _parse_text_track(fh, moov, trak)
    except Exception:
        logger.exception("MP4 chapter scan failed for %s", path)
        return None

    if len(raw) < 2 and not any(title for _, title in raw):
        # A single untitled entry is no better than the one-chapter fallback.
        return None
    chapters = []
    for i, (start, title) in enumerate(raw):
        chapters.append(
            {
                "id": i,
                "start": start,
                "end": raw[i + 1][0] if i + 1 < len(raw) else None,
                "title": title or f"Chapter {i + 1}",
            }
        )
    return chapters
