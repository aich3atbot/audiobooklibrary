import json
import shutil

import pytest
from mutagen.id3 import CHAP, CTOC, ID3, TIT2, TXXX, CTOCFlags

from app.models import (
    AppState,
    AudioFile,
    Author,
    Book,
    DownloadState,
    Edition,
    MediaProgress,
    Release,
    Series,
)
from app.services.audio_meta import (
    _marker_seconds,
    _overdrive_chapters,
    scan_edition_audio,
    scan_missing,
)

OVERDRIVE_XML = (
    "<Markers>"
    "<Marker><Name>Chapter One</Name><Time>0:00.000</Time></Marker>"
    "<Marker><Name>Chapter Two</Name><Time>0:01.500</Time></Marker>"
    "<Marker><Name>Chapter Three</Name><Time>1:02:03.250</Time></Marker>"
    "</Markers>"
)

# A silent MPEG1 Layer III frame: 128 kbps, 44.1 kHz, no padding -> 417 bytes,
# 1152 samples (~26.12ms) per frame. Enough for mutagen to parse duration.
SILENT_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
FRAME_SECONDS = 1152 / 44100


def write_mp3(path, frames=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SILENT_MP3_FRAME * frames)
    return frames * FRAME_SECONDS


@pytest.fixture
def clean_db(db_session):
    for model in (AudioFile, MediaProgress, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def imported_edition(clean_db, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    if lib.exists():
        shutil.rmtree(lib)
    author = Author(hardcover_id=500, name="Test Author")
    book = Book(hardcover_id=1000, title="Test Book", author=author)
    edition = Edition(
        book=book,
        download_state=DownloadState.IMPORTED,
        library_path=str(lib),
    )
    clean_db.add_all([book, edition])
    clean_db.commit()
    return edition


def test_scan_orders_tracks_naturally_and_reads_duration(clean_db, imported_edition, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    expected = write_mp3(lib / "Part 10.mp3", frames=50)
    write_mp3(lib / "Part 2.mp3", frames=100)
    write_mp3(lib / "Part 1.mp3", frames=100)
    (lib / "cover.jpg").write_bytes(b"img")

    count = scan_edition_audio(clean_db, imported_edition)

    assert count == 3
    files = imported_edition.audio_files
    assert [f.rel_path for f in files] == ["Part 1.mp3", "Part 2.mp3", "Part 10.mp3"]
    assert [f.index for f in files] == [1, 2, 3]
    assert files[2].duration == pytest.approx(expected, rel=0.01)
    assert files[0].mime_type == "audio/mpeg"
    assert files[0].size == len(SILENT_MP3_FRAME) * 100


def test_rescan_replaces_rows(clean_db, imported_edition, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    write_mp3(lib / "one.mp3")
    scan_edition_audio(clean_db, imported_edition)
    (lib / "two.mp3").write_bytes(SILENT_MP3_FRAME * 10)

    scan_edition_audio(clean_db, imported_edition)

    assert len(imported_edition.audio_files) == 2


def test_scan_handles_missing_path(clean_db, imported_edition):
    imported_edition.library_path = "/nonexistent/nowhere"
    clean_db.commit()
    assert scan_edition_audio(clean_db, imported_edition) == 0


def test_scan_missing_backfills_only_unscanned(clean_db, imported_edition, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    write_mp3(lib / "one.mp3")

    assert scan_missing() == 1
    # second run: nothing left to backfill
    assert scan_missing() == 0


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0:00.000", 0.0),
        ("12.500", 12.5),
        ("2:03", 123.0),
        ("1:02:03.250", 3723.25),
        ("  0:30  ", 30.0),
        ("", None),
        ("not a time", None),
        ("1:2:3:4", None),
    ],
)
def test_overdrive_marker_times(text, expected):
    assert _marker_seconds(text) == expected


def test_overdrive_chapters_from_tags():
    tags = ID3()
    tags.add(TXXX(desc="OverDrive MediaMarkers", text=[OVERDRIVE_XML]))

    chapters = _overdrive_chapters(tags)

    assert [c["title"] for c in chapters] == ["Chapter One", "Chapter Two", "Chapter Three"]
    assert [c["start"] for c in chapters] == [0.0, 1.5, 3723.25]
    # ends close on the next marker; the last stays open for the track duration
    assert [c["end"] for c in chapters] == [1.5, 3723.25, None]


def test_overdrive_chapters_ignore_junk():
    tags = ID3()
    tags.add(TXXX(desc="OverDrive MediaMarkers", text=["<Markers><oops"]))
    tags.add(TXXX(desc="Something Else", text=[OVERDRIVE_XML]))

    assert _overdrive_chapters(tags) is None


def test_overdrive_chapters_drop_repeated_marker():
    """A chapter split across files repeats its marker; a second one at the
    same instant would make a zero-length chapter."""
    tags = ID3()
    tags.add(
        TXXX(
            desc="OverDrive MediaMarker",  # singular: some rips write it this way
            text=[
                "<Markers>"
                "<Marker><Name>Chapter One</Name><Time>0:00.000</Time></Marker>"
                "<Marker><Name>Chapter One (continued)</Name><Time>0:00.000</Time></Marker>"
                "</Markers>"
            ],
        )
    )

    chapters = _overdrive_chapters(tags)

    assert [c["title"] for c in chapters] == ["Chapter One"]


def test_scan_reads_overdrive_markers_and_title(clean_db, imported_edition, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    path = lib / "Part 1.mp3"
    write_mp3(path, frames=200)
    tags = ID3()
    tags.add(TXXX(desc="OverDrive MediaMarkers", text=[OVERDRIVE_XML]))
    tags.add(TIT2(text=["The Boy Who Lived"]))
    tags.save(path)

    scan_edition_audio(clean_db, imported_edition)

    file = imported_edition.audio_files[0]
    assert file.title == "The Boy Who Lived"
    chapters = json.loads(file.chapters_json)
    assert [c["title"] for c in chapters] == ["Chapter One", "Chapter Two", "Chapter Three"]
    # an open final end is closed with the track's duration
    assert chapters[-1]["end"] == pytest.approx(file.duration)


def test_id3_chapters_beat_overdrive_markers(clean_db, imported_edition, test_settings):
    """CHAP frames are the real thing; markers are the fallback."""
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    path = lib / "Part 1.mp3"
    write_mp3(path, frames=200)
    tags = ID3()
    tags.add(TXXX(desc="OverDrive MediaMarkers", text=[OVERDRIVE_XML]))
    tags.add(CHAP(element_id="ch0", start_time=0, end_time=1000,
                  sub_frames=[TIT2(text=["From CHAP"])]))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                  child_element_ids=["ch0"]))
    tags.save(path)

    scan_edition_audio(clean_db, imported_edition)

    chapters = json.loads(imported_edition.audio_files[0].chapters_json)
    assert [c["title"] for c in chapters] == ["From CHAP"]


def test_scan_leaves_title_null_without_a_tag(clean_db, imported_edition, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    write_mp3(lib / "Part 1.mp3")

    scan_edition_audio(clean_db, imported_edition)

    assert imported_edition.audio_files[0].title is None
