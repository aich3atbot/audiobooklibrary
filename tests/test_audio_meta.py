import shutil

import pytest

from app.models import (
    AppState,
    AudioFile,
    Author,
    Book,
    DownloadState,
    MediaProgress,
    ReadState,
    Release,
    Series,
)
from app.services.audio_meta import scan_book_audio, scan_missing

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
    for model in (AudioFile, MediaProgress, Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def imported_book(clean_db, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    if lib.exists():
        shutil.rmtree(lib)
    author = Author(hardcover_id=500, name="Test Author")
    book = Book(
        hardcover_id=1000,
        title="Test Book",
        author=author,
        read_state=ReadState.READ,
        download_state=DownloadState.IMPORTED,
        library_path=str(lib),
    )
    clean_db.add(book)
    clean_db.commit()
    return book


def test_scan_orders_tracks_naturally_and_reads_duration(clean_db, imported_book, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    expected = write_mp3(lib / "Part 10.mp3", frames=50)
    write_mp3(lib / "Part 2.mp3", frames=100)
    write_mp3(lib / "Part 1.mp3", frames=100)
    (lib / "cover.jpg").write_bytes(b"img")

    count = scan_book_audio(clean_db, imported_book)

    assert count == 3
    files = imported_book.audio_files
    assert [f.rel_path for f in files] == ["Part 1.mp3", "Part 2.mp3", "Part 10.mp3"]
    assert [f.index for f in files] == [1, 2, 3]
    assert files[2].duration == pytest.approx(expected, rel=0.01)
    assert files[0].mime_type == "audio/mpeg"
    assert files[0].size == len(SILENT_MP3_FRAME) * 100


def test_rescan_replaces_rows(clean_db, imported_book, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    write_mp3(lib / "one.mp3")
    scan_book_audio(clean_db, imported_book)
    (lib / "two.mp3").write_bytes(SILENT_MP3_FRAME * 10)

    scan_book_audio(clean_db, imported_book)

    assert len(imported_book.audio_files) == 2


def test_scan_handles_missing_path(clean_db, imported_book):
    imported_book.library_path = "/nonexistent/nowhere"
    clean_db.commit()
    assert scan_book_audio(clean_db, imported_book) == 0


def test_scan_missing_backfills_only_unscanned(clean_db, imported_book, test_settings):
    lib = test_settings.library_dir / "Test Author" / "Test Book"
    write_mp3(lib / "one.mp3")

    assert scan_missing() == 1
    # second run: nothing left to backfill
    assert scan_missing() == 0
