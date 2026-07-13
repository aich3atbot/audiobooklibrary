import os
import time
from datetime import datetime, timezone

import pytest

from app.models import AppState, Author, Book, DownloadState, ReadState, Release, Series
from app.services.importer import (
    import_release,
    library_dir_for,
    matches,
    sanitize,
    scan_downloads_once,
)


@pytest.fixture
def clean_db(db_session):
    for model in (Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def clean_dirs(test_settings):
    for d in (test_settings.download_dir, test_settings.library_dir):
        if d.exists():
            for child in d.iterdir():
                import shutil

                shutil.rmtree(child) if child.is_dir() else child.unlink()
        else:
            d.mkdir(parents=True)
    return test_settings


@pytest.fixture
def book(clean_db):
    author = Author(hardcover_id=500, name="Ryan Rimmel")
    series = Series(hardcover_id=300, name="Noobtown")
    book = Book(
        hardcover_id=646489,
        title="The Mayor of Noobtown",
        author=author,
        series=series,
        series_index=1.0,
        read_state=ReadState.WANT_TO_READ,
        download_state=DownloadState.GRABBED,
    )
    clean_db.add(book)
    clean_db.commit()
    return book


@pytest.fixture
def release(clean_db, book):
    release = Release(
        book=book,
        guid="http://abb.test/abss/mayor-of-noobtown/",
        indexer="AudioBookBay",
        title="The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]",
        size=1000,
        grabbed_at=datetime.now(timezone.utc),
        status="grabbed",
    )
    clean_db.add(release)
    clean_db.commit()
    return release


def make_download(root, name, files=("Book Part 01.mp3", "Book Part 02.mp3", "cover.jpg"),
                  age_seconds=600):
    folder = root / name
    folder.mkdir(parents=True)
    old = time.time() - age_seconds
    for f in files:
        p = folder / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"data")
        os.utime(p, (old, old))
    os.utime(folder, (old, old))
    return folder


def test_sanitize():
    assert sanitize('A:B/C*D?"E<F>G|H\\I') == "A B C D E F G H I"
    assert sanitize("  Trailing dots... ") == "Trailing dots"
    assert sanitize("") == "Unknown"


def test_matches():
    assert matches("The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]",
                   "The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]")
    assert matches("The Mayor of Noobtown - Ryan Rimmel [M4B]",
                   "The.Mayor.of.Noobtown.-.Ryan.Rimmel.[M4B]")
    # containment: folder name is a shortened form of the release title
    assert matches("The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]",
                   "The Mayor of Noobtown - Ryan Rimmel")
    assert not matches("The Mayor of Noobtown", "Completely Different Audiobook")
    assert not matches("abc", "abcdef")  # too short for containment


def test_library_dir_for_with_series(book, clean_dirs):
    path = library_dir_for(book)
    assert path == clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"


def test_library_dir_for_without_series(book, clean_dirs):
    book.series = None
    book.series_index = None
    assert library_dir_for(book) == clean_dirs.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown"


def test_import_release_directory(clean_db, book, release, clean_dirs):
    source = make_download(
        clean_dirs.download_dir,
        "The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]",
        files=("Part 01.mp3", "Part 02.mp3", "cover.jpg", "notes.nfo", "junk.exe"),
    )

    assert import_release(clean_db, release, source) is True

    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert sorted(p.name for p in dest.iterdir()) == [
        "Part 01.mp3", "Part 02.mp3", "cover.jpg", "notes.nfo",
    ]
    assert book.download_state == DownloadState.IMPORTED
    assert book.library_path == str(dest)
    assert release.status == "imported"
    # copy mode leaves the download in place (seeding)
    assert source.exists()


def test_import_release_single_file(clean_db, book, release, clean_dirs):
    source = clean_dirs.download_dir / "The Mayor of Noobtown.m4b"
    source.write_bytes(b"audio")

    assert import_release(clean_db, release, source) is True

    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert (dest / "The Mayor of Noobtown.m4b").exists()


def test_import_release_no_audio_fails(clean_db, book, release, clean_dirs):
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown",
                           files=("readme.txt", "cover.jpg"))

    assert import_release(clean_db, release, source) is False

    assert release.status == "failed"
    assert "no audio files" in release.error
    assert book.download_state == DownloadState.FAILED


def test_import_release_existing_destination_fails(clean_db, book, release, clean_dirs):
    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    dest.mkdir(parents=True)
    (dest / "already.mp3").write_bytes(b"x")
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown")

    assert import_release(clean_db, release, source) is False
    assert "destination already exists" in release.error


def test_scan_imports_stable_download(clean_db, book, release, clean_dirs):
    make_download(clean_dirs.download_dir,
                  "The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]")

    counts = scan_downloads_once()

    assert counts == {"matched": 1, "imported": 1, "failed": 0}
    clean_db.refresh(release)
    clean_db.refresh(book)
    assert release.status == "imported"
    assert book.download_state == DownloadState.IMPORTED


def test_scan_marks_fresh_download_as_downloading(clean_db, book, release, clean_dirs):
    make_download(clean_dirs.download_dir,
                  "The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]", age_seconds=0)

    counts = scan_downloads_once()

    assert counts == {"matched": 1, "imported": 0, "failed": 0}
    clean_db.refresh(release)
    assert release.status == "downloading"
    clean_db.refresh(book)
    assert book.download_state == DownloadState.DOWNLOADING


def test_scan_skips_incomplete_markers(clean_db, book, release, clean_dirs):
    make_download(clean_dirs.download_dir,
                  "The Mayor of Noobtown - Ryan Rimmel [M4B] [32 Kbps]",
                  files=("Part 01.mp3", "Part 02.mp3.part"))

    counts = scan_downloads_once()

    assert counts["imported"] == 0
    clean_db.refresh(release)
    assert release.status == "downloading"


def test_scan_no_match_leaves_release_alone(clean_db, book, release, clean_dirs):
    make_download(clean_dirs.download_dir, "Some Other Audiobook Entirely")

    counts = scan_downloads_once()

    assert counts == {"matched": 0, "imported": 0, "failed": 0}
    clean_db.refresh(release)
    assert release.status == "grabbed"


def test_activity_page(client, clean_db, book, release):
    response = client.get("/activity")
    assert response.status_code == 200
    assert "The Mayor of Noobtown" in response.text
    assert "grabbed" in response.text


def test_cancel_release(client, clean_db, book, release):
    response = client.post(f"/releases/{release.id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    clean_db.refresh(release)
    clean_db.refresh(book)
    assert release.status == "cancelled"
    assert book.download_state == DownloadState.NONE


def test_retry_failed_release(client, clean_db, book, release):
    release.status = "failed"
    release.error = "boom"
    book.download_state = DownloadState.FAILED
    clean_db.commit()

    response = client.post(f"/releases/{release.id}/retry", follow_redirects=False)

    assert response.status_code == 303
    clean_db.refresh(release)
    assert release.status == "grabbed"
    assert release.error is None


def test_manual_import(client, clean_db, book, release, clean_dirs):
    make_download(clean_dirs.download_dir, "Oddly Named Folder")

    response = client.post(
        f"/releases/{release.id}/manual-import",
        data={"folder": "Oddly Named Folder"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    clean_db.refresh(release)
    assert release.status == "imported"


def test_manual_import_rejects_traversal(client, clean_db, book, release, clean_dirs):
    response = client.post(
        f"/releases/{release.id}/manual-import",
        data={"folder": "../outside"},
        follow_redirects=False,
    )
    assert response.status_code == 400
