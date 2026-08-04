"""Identifying audio by contents rather than extension.

The case these fixtures reproduce is real: an AudioBookBay release shipped a
22-hour audiobook named `.mp4` whose bytes are a raw ADTS AAC stream. mutagen's
own sniffing scores on the *filename*, so `mutagen.File()` returns None for it
and the importer used to reject the download outright.
"""

import mutagen
import pytest

from app.services.audio_format import (
    corrected_name,
    has_video_track,
    identify,
)
from app.services.audio_meta import scan_edition_audio
from app.services.importer import collect_files, import_release
from tests.test_audio_meta import clean_db, imported_edition, write_mp3  # noqa: F401
from tests.test_importer import (  # noqa: F401
    book,
    clean_dirs,
    edition,
    make_download,
    release,
)
from tests.test_mp4_chapters import box, u32

# --- fixtures --------------------------------------------------------------

ADTS_FRAME_BYTES = 373
ADTS_FRAME_SECONDS = 1024 / 44100  # one AAC frame is 1024 samples


def adts_bytes(frames: int = 200) -> bytes:
    """A raw ADTS AAC stream: AAC-LC, 44.1 kHz, stereo, no CRC. This is what
    hides behind the release's `.mp4` extension."""
    header = bytes(
        [
            0xFF,
            0xF1,  # sync, MPEG-4, layer 0, no CRC
            (0b01 << 6) | (4 << 2),  # AAC-LC, 44100 Hz
            (0b10 << 6) | ((ADTS_FRAME_BYTES >> 11) & 0x03),  # stereo
            (ADTS_FRAME_BYTES >> 3) & 0xFF,
            ((ADTS_FRAME_BYTES & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )
    return (header + b"\x00" * (ADTS_FRAME_BYTES - len(header))) * frames


def write_adts(path, frames: int = 200) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(adts_bytes(frames))
    return frames * ADTS_FRAME_SECONDS


def mp4_bytes(handler: bytes, timescale: int = 1000, duration: int = 5000) -> bytes:
    """A minimal MP4 container holding a single track of the given handler
    type (`soun` for audio, `vide` for video)."""
    trak = box(
        b"trak",
        box(b"tkhd", u32(0) + u32(0) + u32(0) + u32(1)),
        box(
            b"mdia",
            box(b"mdhd", u32(0) + u32(0) + u32(0) + u32(timescale) + u32(duration)),
            box(b"hdlr", u32(0) + u32(0) + handler + u32(0)),
        ),
    )
    return box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(
        b"moov", box(b"mvhd", u32(0) + u32(0) + u32(0) + u32(timescale) + u32(duration)), trak
    )


def write_mp4(path, handler: bytes = b"soun"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mp4_bytes(handler))
    return path


# --- identification --------------------------------------------------------


def test_mutagen_alone_cannot_identify_the_release(tmp_path):
    """The premise: this is why identify() has to exist at all."""
    path = tmp_path / "Some Audiobook.mp4"
    write_adts(path)
    assert mutagen.File(path) is None


def test_identify_reads_adts_hiding_behind_an_mp4_extension(tmp_path):
    path = tmp_path / "Some Audiobook.mp4"
    expected = write_adts(path)

    fmt = identify(path)
    assert fmt is not None
    assert fmt.family == "aac"
    assert fmt.mime == "audio/aac"
    assert fmt.duration == pytest.approx(expected, rel=0.02)


def test_identify_trusts_a_real_mp4_container(tmp_path):
    path = write_mp4(tmp_path / "Real.mp4")
    fmt = identify(path)
    assert fmt is not None
    assert fmt.family == "mp4"
    assert fmt.mime == "audio/mp4"


def test_identify_returns_none_for_junk(tmp_path):
    path = tmp_path / "notes.mp3"
    path.write_bytes(b"not audio at all")
    assert identify(path) is None


def test_has_video_track_only_fires_on_real_video(tmp_path):
    assert has_video_track(write_mp4(tmp_path / "trailer.mp4", handler=b"vide"))
    assert not has_video_track(write_mp4(tmp_path / "book.mp4", handler=b"soun"))
    # a non-MP4 has no handlers at all, and must never be called video
    adts = tmp_path / "book.aac"
    write_adts(adts)
    assert not has_video_track(adts)


def test_rename_only_fires_when_the_family_is_wrong(tmp_path):
    """.m4b, .m4a and .mp4 are all MP4 containers, so none is ever rewritten
    into another — only a genuine mismatch is corrected."""
    for name in ("Book.m4b", "Book.m4a", "Book.mp4"):
        path = write_mp4(tmp_path / name)
        fmt = identify(path)
        assert corrected_name(path, fmt) == name

    mislabelled = tmp_path / "Book 2.mp4"
    write_adts(mislabelled)
    assert corrected_name(mislabelled, identify(mislabelled)) == "Book 2.aac"


# --- the importer ----------------------------------------------------------


def test_single_mislabelled_file_imports_and_is_renamed(
    clean_db, edition, release, clean_dirs  # noqa: F811
):
    """The reported bug, end to end: a lone .mp4 that is really AAC."""
    source = clean_dirs.download_dir / "The Mayor of Noobtown (Noobtown, Book 1).mp4"
    write_adts(source)

    assert import_release(clean_db, release, source) is True

    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert [p.name for p in dest.iterdir()] == [
        "The Mayor of Noobtown (Noobtown, Book 1).aac"
    ]
    assert release.status == "imported"
    assert release.error is None
    # hardlink mode is the default: the download stays put, still seeding
    assert source.exists()


def test_audio_mp4_imports_and_keeps_its_extension(
    clean_db, edition, release, clean_dirs  # noqa: F811
):
    source = clean_dirs.download_dir / "Noobtown.mp4"
    write_mp4(source)

    assert import_release(clean_db, release, source) is True

    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert [p.name for p in dest.iterdir()] == ["Noobtown.mp4"]


def test_video_sample_is_left_out_of_a_folder_import(clean_dirs):  # noqa: F811
    folder = clean_dirs.download_dir / "Noobtown [M4B]"
    folder.mkdir(parents=True)
    write_mp4(folder / "Book.mp4", handler=b"soun")
    write_mp4(folder / "trailer.mp4", handler=b"vide")
    (folder / "cover.jpg").write_bytes(b"img")

    names = sorted(dest.name for _, dest in collect_files(folder))
    assert names == ["Book.mp4", "cover.jpg"]


def test_a_video_only_release_fails_rather_than_importing(
    clean_db, edition, release, clean_dirs  # noqa: F811
):
    source = clean_dirs.download_dir / "Noobtown.mp4"
    write_mp4(source, handler=b"vide")

    assert import_release(clean_db, release, source) is False
    assert release.status == "failed"
    assert "no audio files found" in release.error


def test_unparseable_audio_still_imports_under_its_own_name(clean_dirs):  # noqa: F811
    """Identification rules files out; it never gatekeeps one that would
    otherwise import. An unreadable header costs metadata, not the audiobook."""
    folder = make_download(
        clean_dirs.download_dir, "Noobtown [M4B]", files=("Part 01.m4b", "Part 02.mp3")
    )
    names = sorted(dest.name for _, dest in collect_files(folder))
    assert names == ["Part 01.m4b", "Part 02.mp3"]


# --- the audio scanner -----------------------------------------------------


def test_scan_reads_duration_and_mime_from_contents(
    clean_db, imported_edition, test_settings  # noqa: F811
):
    """A file the importer never renamed — placed by an older build or by hand
    — must still get a real duration instead of a null one."""
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    expected = write_adts(lib / "Book.mp4")

    assert scan_edition_audio(clean_db, imported_edition) == 1

    track = imported_edition.audio_files[0]
    assert track.duration == pytest.approx(expected, rel=0.02)
    assert track.mime_type == "audio/aac"


def test_scan_keeps_mime_types_for_ordinary_files(
    clean_db, imported_edition, test_settings  # noqa: F811
):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    write_mp3(lib / "Part 1.mp3")
    write_mp4(lib / "Part 2.m4b")

    assert scan_edition_audio(clean_db, imported_edition) == 2
    by_name = {f.rel_path: f.mime_type for f in imported_edition.audio_files}
    assert by_name == {"Part 1.mp3": "audio/mpeg", "Part 2.m4b": "audio/mp4"}


def test_a_rename_never_clobbers_a_sibling(clean_dirs):  # noqa: F811
    """Two files, one correctly named .aac and one .mp4 that is also AAC: the
    rename must stand down rather than overwrite the sibling."""
    folder = clean_dirs.download_dir / "Noobtown [M4B]"
    folder.mkdir(parents=True)
    write_adts(folder / "Book.aac")
    write_adts(folder / "Book.mp4")

    names = sorted(dest.name for _, dest in collect_files(folder))
    assert names == ["Book.aac", "Book.mp4"]
