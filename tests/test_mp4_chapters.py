"""MP4 (m4b) chapter extraction: Nero `chpl` and QuickTime chapter tracks.

The fixtures build real MP4 box structures byte by byte — small enough to
read, and they exercise the parser the way a rip does.
"""

import json

import pytest

from app.services.audio_meta import (
    CHAPTER_SCAN_KEY,
    CHAPTER_SCAN_VERSION,
    rescan_for_chapters,
    scan_edition_audio,
)
from app.services.mp4_chapters import read_mp4_chapters
from tests.test_audio_meta import clean_db, imported_edition  # noqa: F401


def box(box_type: bytes, *payloads: bytes) -> bytes:
    payload = b"".join(payloads)
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def chpl(chapters: list[tuple[float, str]], version: int = 1) -> bytes:
    """Nero chapter list; starts are seconds, stored as 100ns ticks."""
    payload = bytes([version]) + b"\x00\x00\x00"
    if version:
        payload += u32(0)
    payload += bytes([len(chapters)])
    for start, title in chapters:
        encoded = title.encode()
        payload += int(start * 10_000_000).to_bytes(8, "big")
        payload += bytes([len(encoded)]) + encoded
    return box(b"chpl", payload)


def nero_m4b(path, chapters, version: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        box(b"ftyp", b"M4A \x00\x00\x00\x00")
        + box(b"moov", box(b"udta", chpl(chapters, version)))
    )
    return path


def quicktime_m4b(path, chapters: list[tuple[float, str]], timescale: int = 1000):
    """An audio trak pointing at a text trak whose samples are the titles."""
    samples = [
        len(title.encode()).to_bytes(2, "big") + title.encode() for _, title in chapters
    ]
    sizes = [len(s) for s in samples]

    ftyp = box(b"ftyp", b"M4A \x00\x00\x00\x00")
    mdat_payload = b"".join(samples)
    first_sample_offset = len(ftyp) + 8  # after the mdat header

    deltas = []
    for i, (start, _) in enumerate(chapters):
        nxt = chapters[i + 1][0] if i + 1 < len(chapters) else start + 10
        deltas.append(int((nxt - start) * timescale))

    stts = box(b"stts", u32(0) + u32(len(deltas)) + b"".join(u32(1) + u32(d) for d in deltas))
    stsz = box(b"stsz", u32(0) + u32(0) + u32(len(sizes)) + b"".join(u32(s) for s in sizes))
    stsc = box(b"stsc", u32(0) + u32(1) + u32(1) + u32(len(sizes)) + u32(1))
    stco = box(b"stco", u32(0) + u32(1) + u32(first_sample_offset))
    stbl = box(b"stbl", stts, stsc, stsz, stco)

    text_trak = box(
        b"trak",
        box(b"tkhd", u32(0) + u32(0) + u32(0) + u32(2)),
        box(
            b"mdia",
            box(b"mdhd", u32(0) + u32(0) + u32(0) + u32(timescale)),
            box(b"hdlr", u32(0) + u32(0) + b"text" + u32(0)),
            box(b"minf", stbl),
        ),
    )
    audio_trak = box(
        b"trak",
        box(b"tkhd", u32(0) + u32(0) + u32(0) + u32(1)),
        box(b"tref", box(b"chap", u32(2))),
        box(b"mdia", box(b"hdlr", u32(0) + u32(0) + b"soun" + u32(0))),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ftyp + box(b"mdat", mdat_payload) + box(b"moov", audio_trak, text_trak))
    return path


CHAPTERS = [(0.0, "Prologue"), (61.5, "Chapter One"), (200.0, "Chapter Two")]


def test_reads_nero_chpl(tmp_path):
    chapters = read_mp4_chapters(nero_m4b(tmp_path / "book.m4b", CHAPTERS))

    assert [c["title"] for c in chapters] == ["Prologue", "Chapter One", "Chapter Two"]
    assert [c["id"] for c in chapters] == [0, 1, 2]
    assert chapters[1]["start"] == pytest.approx(61.5)
    # each chapter runs to the next one; the last is left open for the caller
    assert chapters[0]["end"] == pytest.approx(61.5)
    assert chapters[1]["end"] == pytest.approx(200.0)
    assert chapters[2]["end"] is None


def test_reads_nero_chpl_version_zero(tmp_path):
    """Version 0 has no reserved word before the count."""
    chapters = read_mp4_chapters(nero_m4b(tmp_path / "v0.m4b", CHAPTERS, version=0))
    assert [c["title"] for c in chapters] == ["Prologue", "Chapter One", "Chapter Two"]


def test_reads_quicktime_chapter_track(tmp_path):
    chapters = read_mp4_chapters(quicktime_m4b(tmp_path / "qt.m4b", CHAPTERS))

    assert [c["title"] for c in chapters] == ["Prologue", "Chapter One", "Chapter Two"]
    assert chapters[1]["start"] == pytest.approx(61.5)
    assert chapters[0]["end"] == pytest.approx(61.5)


def test_reads_chapter_track_with_64bit_offsets_and_multiple_chunks(tmp_path):
    """The alternate encodings real muxers emit: co64 instead of stco, samples
    spread over several chunks, and version-1 tkhd/mdhd headers."""
    timescale = 600
    samples = [
        len(title.encode()).to_bytes(2, "big") + title.encode() for _, title in CHAPTERS
    ]
    sizes = [len(s) for s in samples]
    ftyp = box(b"ftyp", b"M4A \x00\x00\x00\x00")
    # two chunks: the first holds samples 1-2, the second sample 3
    chunk_one = b"".join(samples[:2])
    chunk_two = samples[2]
    chunk_one_offset = len(ftyp) + 8
    chunk_two_offset = chunk_one_offset + len(chunk_one)

    deltas = []
    for i, (start, _) in enumerate(CHAPTERS):
        nxt = CHAPTERS[i + 1][0] if i + 1 < len(CHAPTERS) else start + 10
        deltas.append(int((nxt - start) * timescale))

    stbl = box(
        b"stbl",
        box(b"stts", u32(0) + u32(len(deltas)) + b"".join(u32(1) + u32(d) for d in deltas)),
        box(b"stsc", u32(0) + u32(2) + u32(1) + u32(2) + u32(1) + u32(2) + u32(1) + u32(1)),
        box(b"stsz", u32(0) + u32(0) + u32(len(sizes)) + b"".join(u32(s) for s in sizes)),
        box(
            b"co64",
            u32(0) + u32(2)
            + chunk_one_offset.to_bytes(8, "big")
            + chunk_two_offset.to_bytes(8, "big"),
        ),
    )
    text_trak = box(
        b"trak",
        box(b"tkhd", bytes([1]) + b"\x00\x00\x00" + b"\x00" * 16 + u32(2)),
        box(
            b"mdia",
            box(b"mdhd", bytes([1]) + b"\x00\x00\x00" + b"\x00" * 16 + u32(timescale)),
            box(b"hdlr", u32(0) + u32(0) + b"sbtl" + u32(0)),
            box(b"minf", stbl),
        ),
    )
    audio_trak = box(
        b"trak",
        box(b"tkhd", u32(0) + u32(0) + u32(0) + u32(1)),
        box(b"tref", box(b"chap", u32(2))),
    )
    path = tmp_path / "co64.m4b"
    path.write_bytes(
        ftyp + box(b"mdat", chunk_one + chunk_two) + box(b"moov", audio_trak, text_trak)
    )

    chapters = read_mp4_chapters(path)
    assert [c["title"] for c in chapters] == ["Prologue", "Chapter One", "Chapter Two"]
    assert chapters[1]["start"] == pytest.approx(61.5)
    assert chapters[2]["start"] == pytest.approx(200.0)


def test_nero_wins_over_chapter_track(tmp_path):
    path = quicktime_m4b(tmp_path / "both.m4b", CHAPTERS)
    data = bytearray(path.read_bytes())
    # graft a udta/chpl with different titles onto the moov
    moov_start = data.find(b"moov") - 4
    moov_size = int.from_bytes(data[moov_start : moov_start + 4], "big")
    extra = box(b"udta", chpl([(0.0, "Nero One"), (30.0, "Nero Two")]))
    data[moov_start : moov_start + 4] = u32(moov_size + len(extra))
    data[moov_start + moov_size : moov_start + moov_size] = extra
    path.write_bytes(bytes(data))

    assert [c["title"] for c in read_mp4_chapters(path)] == ["Nero One", "Nero Two"]


def test_file_without_chapters(tmp_path):
    path = tmp_path / "plain.m4b"
    path.write_bytes(box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(b"moov", box(b"udta", b"")))
    assert read_mp4_chapters(path) is None


def test_single_untitled_chapter_is_not_worth_reporting(tmp_path):
    path = nero_m4b(tmp_path / "one.m4b", [(0.0, "")])
    assert read_mp4_chapters(path) is None


def test_garbage_never_raises(tmp_path):
    truncated = tmp_path / "truncated.m4b"
    truncated.write_bytes(box(b"ftyp", b"M4A ") + b"\x00\x00\x40\x00moov\x01\x02")
    assert read_mp4_chapters(truncated) is None

    noise = tmp_path / "noise.m4b"
    noise.write_bytes(b"not an mp4 at all")
    assert read_mp4_chapters(noise) is None


def test_scan_stores_m4b_chapters(clean_db, imported_edition, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    nero_m4b(lib / "book.m4b", CHAPTERS)

    assert scan_edition_audio(clean_db, imported_edition) == 1

    file = imported_edition.audio_files[0]
    assert file.mime_type == "audio/mp4"
    chapters = json.loads(file.chapters_json)
    assert [c["title"] for c in chapters] == ["Prologue", "Chapter One", "Chapter Two"]
    # the open final chapter is closed off with the track length (unknown here)
    assert chapters[-1]["end"] is not None


def test_rescan_backfills_older_scans_once(clean_db, imported_edition, test_settings):
    """A library scanned before chapter support must pick chapters up without
    the user re-importing anything — but only once per version."""
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    nero_m4b(lib / "book.m4b", CHAPTERS)
    scan_edition_audio(clean_db, imported_edition)
    file = imported_edition.audio_files[0]
    file.chapters_json = None  # as an older build left it
    clean_db.commit()

    assert rescan_for_chapters() == 1
    clean_db.expire_all()
    assert len(json.loads(imported_edition.audio_files[0].chapters_json)) == 3

    # the version marker keeps it from running again
    assert rescan_for_chapters() == 0

    from app.models import AppState

    assert clean_db.get(AppState, CHAPTER_SCAN_KEY).value == CHAPTER_SCAN_VERSION
