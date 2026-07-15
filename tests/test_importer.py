import os
import time
from datetime import datetime, timezone

import pytest

from app.models import AppState, Author, Book, DownloadState, Release, Series, UserBook
from app.services.importer import (
    import_release,
    library_dir_for,
    matches,
    replace_key,
    sanitize,
    scan_downloads_once,
)
from app.services.sync import get_state, set_state


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Book, Author, Series, AppState):
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


def make_replaced_book(db, book, release, old_dir):
    """An already-imported book whose release is marked as a replacement."""
    old_dir.mkdir(parents=True)
    (old_dir / "old.mp3").write_bytes(b"old")
    book.download_state = DownloadState.IMPORTED
    book.library_path = str(old_dir)
    set_state(db, replace_key(release), "1")
    db.commit()


def test_import_replace_clears_old_files_first(clean_db, book, release, clean_dirs):
    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    make_replaced_book(clean_db, book, release, dest)
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown")

    assert import_release(clean_db, release, source) is True

    assert not (dest / "old.mp3").exists()
    assert (dest / "Book Part 01.mp3").exists()
    assert book.download_state == DownloadState.IMPORTED
    assert book.library_path == str(dest)
    assert get_state(clean_db, replace_key(release)) is None


def test_import_replace_removes_old_path_when_it_differs(clean_db, book, release, clean_dirs):
    old = clean_dirs.library_dir / "Old Author" / "Old Title"
    make_replaced_book(clean_db, book, release, old)
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown")

    assert import_release(clean_db, release, source) is True

    assert not old.parent.exists()  # old dir gone, empty parents cleaned up
    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert (dest / "Book Part 01.mp3").exists()
    assert book.library_path == str(dest)


def test_import_replace_failure_keeps_old_files(clean_db, book, release, clean_dirs):
    dest = clean_dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    make_replaced_book(clean_db, book, release, dest)
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown",
                           files=("readme.txt",))

    assert import_release(clean_db, release, source) is False

    assert (dest / "old.mp3").exists()
    assert release.status == "failed"
    # the old files survived, so the book is still genuinely available
    assert book.download_state == DownloadState.IMPORTED
    assert book.library_path == str(dest)
    assert get_state(clean_db, replace_key(release)) == "1"


class FakeDownloadClient:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.removed: list[tuple[str, bool]] = []

    def remove_torrent(self, info_hash, remove_data=True):
        if self.fail:
            raise RuntimeError("client unreachable")
        self.removed.append((info_hash, remove_data))

    def __enter__(self):
        return self
    def __exit__(self, *exc):
        pass


@pytest.fixture
def remove_immediately(test_settings, monkeypatch):
    """Flag on, with a fake download client capturing removals."""
    import app.services.downloads as downloads

    monkeypatch.setattr(test_settings, "download_remove_immediately", True)
    monkeypatch.setattr(test_settings, "download_url", "http://deluge.test:8112")
    fake = FakeDownloadClient()
    monkeypatch.setattr(downloads, "get_download_client", lambda **kw: fake)
    return fake


def test_import_removes_torrent_when_configured(clean_db, book, release, clean_dirs,
                                                remove_immediately):
    release.info_hash = "a" * 40
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown")

    assert import_release(clean_db, release, source) is True

    assert remove_immediately.removed == [("a" * 40, True)]
    assert release.status == "imported"
    assert release.error is None


def test_failed_removal_never_unimports(clean_db, book, release, clean_dirs,
                                         remove_immediately):
    remove_immediately.fail = True
    release.info_hash = "a" * 40
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown")

    assert import_release(clean_db, release, source) is True

    assert release.status == "imported"
    assert book.download_state == DownloadState.IMPORTED
    assert "may still be seeding" in release.error


def test_import_leaves_torrent_by_default(clean_db, book, release, clean_dirs, monkeypatch):
    import app.services.downloads as downloads

    release.info_hash = "a" * 40
    fake = FakeDownloadClient()
    monkeypatch.setattr(downloads, "get_download_client", lambda **kw: fake)
    source = make_download(clean_dirs.download_dir, "The Mayor of Noobtown")

    assert import_release(clean_db, release, source) is True
    assert fake.removed == []


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


def test_cancel_replace_restores_available(client, clean_db, book, release):
    book.download_state = DownloadState.GRABBED
    book.library_path = "/audiobooks/kept"  # old files never removed (deferred)
    set_state(clean_db, replace_key(release), "1")
    clean_db.commit()

    response = client.post(f"/releases/{release.id}/cancel", follow_redirects=False)

    assert response.status_code == 303
    clean_db.expire_all()
    assert release.status == "cancelled"
    assert book.download_state == DownloadState.IMPORTED
    assert get_state(clean_db, replace_key(release)) is None


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
