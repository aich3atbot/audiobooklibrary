"""The watcher's download-client path: progress, completion, and the fallback
to name matching when the client can't tell us anything."""

import shutil
from datetime import datetime, timezone

import pytest

from app.clients.download_client import DownloadClientError, TorrentStatus
from app.models import AppState, Author, Book, DownloadState, ReadState, Release, Series
from app.services import importer
from app.services.importer import scan_downloads_once

HASH = "ad5fae5ffda056f9f45131045d140326bbafc4dc"
TORRENT_NAME = "The Mayor of Noobtown - Ryan Rimmel [M4B]"


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
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        else:
            d.mkdir(parents=True)
    return test_settings


@pytest.fixture
def deluge_configured(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "download_url", "http://deluge.test:8112")


@pytest.fixture
def book(clean_db):
    author = Author(hardcover_id=500, name="Ryan Rimmel")
    book = Book(
        hardcover_id=646489,
        title="The Mayor of Noobtown",
        author=author,
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
        title=TORRENT_NAME,
        size=1000,
        info_hash=HASH,
        magnet_uri=f"magnet:?xt=urn:btih:{HASH}",
        progress=0.0,
        grabbed_at=datetime.now(timezone.utc),
        status="grabbed",
    )
    clean_db.add(release)
    clean_db.commit()
    return release


def make_download(root, name=TORRENT_NAME, files=("Book.mp3",)):
    """A *fresh* download folder — mtimes are now, so the quiet-period
    heuristic would refuse to import it. Only the client saying "finished" can."""
    folder = root / name
    folder.mkdir(parents=True)
    for filename in files:
        (folder / filename).write_bytes(b"\x00" * 16)
    return folder


class FakeClient:
    """Stands in for Deluge. `statuses` is what get_status returns; `error`
    makes the client unreachable."""

    def __init__(self, statuses=None, error=None):
        self.statuses = statuses or {}
        self.error = error
        self.asked_for = None

    def get_status(self, info_hashes):
        if self.error:
            raise self.error
        self.asked_for = list(info_hashes)
        return {h: s for h, s in self.statuses.items() if h in info_hashes}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def use_client(monkeypatch, client):
    monkeypatch.setattr(importer, "get_download_client", lambda *a, **kw: client)
    return client


def status(**kwargs):
    defaults = dict(
        info_hash=HASH,
        name=TORRENT_NAME,
        progress=0.0,
        state="Downloading",
        is_finished=False,
        save_path="/downloads/complete",  # the client's namespace, not ours
        total_size=1000,
    )
    return TorrentStatus(**{**defaults, **kwargs})


def test_in_progress_torrent_records_progress_and_does_not_import(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch
):
    make_download(clean_dirs.download_dir)
    use_client(monkeypatch, FakeClient({HASH: status(progress=42.5)}))

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert counts == {"matched": 1, "imported": 0, "failed": 0}
    assert release.status == "downloading"
    assert release.progress == 42.5
    assert release.book.download_state == DownloadState.DOWNLOADING


def test_finished_torrent_imports_immediately(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch
):
    make_download(clean_dirs.download_dir)
    client = use_client(
        monkeypatch,
        # A finished torrent can sit in any state — "Queued" here, as real
        # Deluge reports. Completion is is_finished, never the state.
        FakeClient({HASH: status(progress=100.0, state="Queued", is_finished=True)}),
    )

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert client.asked_for == [HASH]
    assert counts["imported"] == 1
    assert release.status == "imported"
    # the freshly-written download would have failed the quiet-period check —
    # the client's word overrides it
    assert release.book.download_state == DownloadState.IMPORTED
    assert (clean_dirs.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown").is_dir()


def test_finished_torrent_missing_from_download_dir_fails_with_a_mapping_hint(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch
):
    # Nothing on disk: DOWNLOAD_DIR isn't where the client actually saves.
    use_client(monkeypatch, FakeClient({HASH: status(progress=100.0, is_finished=True)}))

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert counts["failed"] == 1
    assert release.status == "failed"
    assert release.book.download_state == DownloadState.FAILED
    assert "DOWNLOAD_DIR" in release.error
    assert TORRENT_NAME in release.error


def test_finished_torrent_renamed_by_the_client_is_still_found(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch
):
    # Deluge names the folder from the torrent metadata, which need not equal
    # the release title we recorded.
    make_download(clean_dirs.download_dir, name="mayor.of.noobtown.m4b")
    use_client(
        monkeypatch,
        FakeClient({HASH: status(name="mayor.of.noobtown.m4b", progress=100.0, is_finished=True)}),
    )

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert counts["imported"] == 1
    assert release.status == "imported"


def test_unreachable_client_falls_back_to_the_directory_watcher(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch, test_settings
):
    monkeypatch.setattr(test_settings, "download_quiet_seconds", 0)  # download has settled
    make_download(clean_dirs.download_dir)
    use_client(monkeypatch, FakeClient(error=DownloadClientError("connection refused")))

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert counts["imported"] == 1  # imported by name matching, not by hash
    assert release.status == "imported"


def test_torrent_the_client_does_not_know_falls_back_to_name_matching(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch, test_settings
):
    monkeypatch.setattr(test_settings, "download_quiet_seconds", 0)
    make_download(clean_dirs.download_dir)
    use_client(monkeypatch, FakeClient({}))  # torrent removed from the client

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert counts["imported"] == 1
    assert release.status == "imported"


def test_release_without_an_info_hash_never_asks_the_client(
    clean_db, clean_dirs, deluge_configured, release, monkeypatch, test_settings
):
    # A release grabbed before hashes were recorded (pre-migration).
    release.info_hash = None
    clean_db.commit()
    monkeypatch.setattr(test_settings, "download_quiet_seconds", 0)
    make_download(clean_dirs.download_dir)
    client = use_client(monkeypatch, FakeClient({HASH: status(is_finished=True)}))

    counts = scan_downloads_once()

    clean_db.refresh(release)
    clean_db.refresh(release.book)
    assert client.asked_for is None  # no hashes to ask about
    assert counts["imported"] == 1
    assert release.status == "imported"


def test_progress_is_shown_on_the_activity_page(client, clean_db, release):
    release.status = "downloading"
    release.progress = 37.4
    clean_db.commit()

    response = client.get("/activity")

    assert "37%" in response.text
