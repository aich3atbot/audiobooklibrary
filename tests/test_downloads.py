import json
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from app.models import (
    AppState,
    AudioFile,
    Author,
    Book,
    DownloadState,
    Edition,
    Release,
    Series,
    UserBook,
)
from app.services.downloads import grab_release, search_releases
from app.services.importer import replace_key
from app.services.sync import get_state
from tests.conftest import make_user_book
from tests.test_audio_meta import write_mp3

ABB = "http://abb.test"
DELUGE = "http://deluge.test:8112"
GUID = f"{ABB}/abss/mayor-of-noobtown/"
HASH = "ad5fae5ffda056f9f45131045d140326bbafc4dc"
FIXTURES = Path(__file__).parent / "fixtures" / "abb"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def download_config(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "index_url", ABB)
    monkeypatch.setattr(test_settings, "download_url", DELUGE)
    monkeypatch.setattr(test_settings, "download_client", "deluge")
    monkeypatch.setattr(test_settings, "download_password", "")


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def book(clean_db, user):
    author = Author(hardcover_id=500, name="Ryan Rimmel")
    book = Book(
        hardcover_id=646489,
        title="The Mayor of Noobtown",
        author=author,
    )
    clean_db.add(book)
    clean_db.commit()
    make_user_book(clean_db, user, book)
    return book


def make_edition(db, book, **kwargs):
    edition = Edition(book=book, **kwargs)
    db.add(edition)
    db.commit()
    return edition


def mock_deluge(add_result=HASH, add_error=None):
    """Answer Deluge's JSON-RPC calls: login, connect, add_torrent_magnet."""

    def handle(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "core.add_torrent_magnet":
            if add_error is not None:
                return httpx.Response(
                    200, json={"result": None, "error": {"message": add_error, "code": 2}}
                )
            return httpx.Response(200, json={"result": add_result, "error": None})
        results = {"auth.login": True, "web.connected": True}
        return httpx.Response(200, json={"result": results[method], "error": None})

    return respx.post(f"{DELUGE}/json").mock(side_effect=handle)


def mock_search():
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(200, text=fixture("search.html")))
    respx.get(f"{ABB}/page/2/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )


def mock_details():
    return respx.get(GUID).mock(return_value=httpx.Response(200, text=fixture("details.html")))


@respx.mock
def test_search_releases_queries_author_and_title(book):
    route = respx.get(f"{ABB}/").mock(
        return_value=httpx.Response(200, text=fixture("search.html"))
    )
    respx.get(f"{ABB}/page/2/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )

    releases = search_releases(book)

    assert route.calls[0].request.url.params["s"] == "ryan rimmel the mayor of noobtown"
    # the indexer's own order is preserved — ABB publishes no seeders to rank on
    assert [r.format for r in releases] == ["M4B", "MP3"]


@respx.mock
def test_search_releases_falls_back_to_title_only(book):
    empty = httpx.Response(200, text=fixture("search_empty.html"))
    hits = httpx.Response(200, text=fixture("search.html"))
    page1 = respx.get(f"{ABB}/").mock(side_effect=[empty, hits])
    respx.get(f"{ABB}/page/2/").mock(return_value=empty)

    releases = search_releases(book)

    assert page1.calls[1].request.url.params["s"] == "the mayor of noobtown"
    assert len(releases) == 2


@respx.mock
def test_grab_release_adds_magnet_and_records_hash(clean_db, user, book):
    mock_details()
    deluge = mock_deluge()

    release = grab_release(clean_db, user, book, GUID, "AudioBookBay", "Noobtown [M4B]", 1000)

    assert release.info_hash == HASH
    assert release.guid == GUID
    assert release.indexer == "AudioBookBay"
    # the details-page title wins: it is what the magnet names the download
    assert release.title == "Project Hail Mary - Andy Weir"
    assert release.magnet_uri.startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert release.status == "grabbed"
    assert release.progress == 0.0
    assert release.user_id == user.id
    assert release.edition.book is book
    assert release.edition.label == ""
    assert release.edition.download_state == DownloadState.GRABBED

    add = json.loads(deluge.calls[-1].request.content)
    assert add["method"] == "core.add_torrent_magnet"
    assert add["params"][0] == release.magnet_uri


@respx.mock
def test_grab_release_survives_a_torrent_deluge_already_has(clean_db, book):
    mock_details()
    mock_deluge(add_error="Torrent already in session")

    release = grab_release(clean_db, None, book, GUID, "AudioBookBay", "Noobtown", None)

    assert release.info_hash == HASH  # recovered from the magnet
    assert release.edition.download_state == DownloadState.GRABBED


@respx.mock
def test_grab_failure_leaves_state_untouched(clean_db, book):
    mock_details()
    mock_deluge(add_error="Invalid magnet")

    with pytest.raises(Exception):
        grab_release(clean_db, None, book, GUID, "AudioBookBay", "Noobtown", None)

    clean_db.rollback()
    assert clean_db.query(Release).count() == 0
    assert all(e.download_state == DownloadState.NONE for e in book.editions)


@respx.mock
def test_releases_route_renders_dialog(client, book):
    mock_search()

    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert "Andy Weir - Project Hail Mary" in response.text
    assert "M4B" in response.text
    assert "14 Nov 2021" in response.text
    assert "Seeders" not in response.text  # ABB publishes none
    assert 'name="indexer" value="AudioBookBay"' in response.text


@respx.mock
def test_releases_route_shows_search_failure(client, book):
    respx.get(f"{ABB}/").mock(side_effect=httpx.ConnectError("down"))

    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert "Search failed" in response.text


@respx.mock
def test_grab_route_closes_modal_and_updates_card(client, clean_db, book):
    mock_details()
    mock_deluge()

    response = client.post(
        f"/books/{book.id}/grab",
        data={
            "guid": GUID,
            "indexer": "AudioBookBay",
            "title": "Noobtown [M4B]",
            "size": "1000",
        },
    )

    assert response.status_code == 200
    assert '<div id="modal"></div>' in response.text  # modal closed
    assert "downloading" in response.text  # out-of-band card swap shows the new badge
    assert clean_db.query(Release).one().info_hash == HASH


@respx.mock
def test_grab_route_failure_shows_error(client, clean_db, book):
    mock_details()
    mock_deluge(add_error="Invalid magnet")

    response = client.post(
        f"/books/{book.id}/grab",
        data={"guid": GUID, "indexer": "AudioBookBay", "title": "Noobtown", "size": ""},
    )

    assert response.status_code == 200
    assert "Grab failed" in response.text
    assert clean_db.query(Release).count() == 0


@respx.mock
def test_grab_blocked_when_book_available(client, clean_db, book):
    make_edition(clean_db, book, download_state=DownloadState.IMPORTED,
                 library_path="/audiobooks/x")

    response = client.post(
        f"/books/{book.id}/grab",
        data={"guid": GUID, "indexer": "AudioBookBay", "title": "Noobtown", "size": ""},
    )

    assert response.status_code == 409
    assert clean_db.query(Release).count() == 0


@respx.mock
def test_grab_blocked_when_book_downloading(client, clean_db, book):
    make_edition(clean_db, book, download_state=DownloadState.DOWNLOADING)

    response = client.post(
        f"/books/{book.id}/grab",
        data={"guid": GUID, "indexer": "AudioBookBay", "title": "Noobtown", "size": ""},
    )

    assert response.status_code == 409


def test_release_picker_refuses_available_book(client, clean_db, book):
    make_edition(clean_db, book, download_state=DownloadState.IMPORTED,
                 library_path="/audiobooks/x")

    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert "already available" in response.text


@pytest.fixture
def imported_book(clean_db, book, test_settings):
    """The book imported for real: files on disk, audio rows, IMPORTED."""
    lib = test_settings.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown"
    if lib.exists():
        shutil.rmtree(lib)
    lib.mkdir(parents=True)
    write_mp3(lib / "Part 01.mp3", frames=100)
    (lib / "cover.jpg").write_bytes(b"img")
    edition = make_edition(clean_db, book, download_state=DownloadState.IMPORTED,
                           library_path=str(lib))
    edition.audio_files.append(
        AudioFile(index=1, rel_path="Part 01.mp3", size=100, mtime_ms=0, mime_type="audio/mpeg")
    )
    clean_db.commit()
    return book


def test_detail_page_lists_editions(client, imported_book):
    edition = imported_book.editions[0]

    response = client.get(f"/books/{imported_book.id}")

    assert response.status_code == 200
    assert "The Mayor of Noobtown" in response.text
    assert edition.library_path in response.text
    assert f"/editions/{edition.id}/rename" in response.text  # rename dialog
    assert f"/editions/{edition.id}/files" in response.text  # lazy file table
    assert f"replace=1&edition_id={edition.id}" in response.text
    assert "Download another edition" in response.text


def test_detail_page_when_not_available(client, book):
    response = client.get(f"/books/{book.id}")

    assert response.status_code == 200
    assert "no downloaded files" in response.text
    assert "replace=1" not in response.text
    assert f"/books/{book.id}/releases?detail=1" in response.text  # Download button


def test_detail_page_shows_downloading_edition(client, clean_db, book):
    make_edition(clean_db, book, download_state=DownloadState.DOWNLOADING)

    response = client.get(f"/books/{book.id}")

    assert response.status_code == 200
    assert "Files will appear here once the download imports." in response.text
    assert "replace=1" not in response.text
    assert "btn-download" not in response.text  # no grab entry while it downloads


def test_detail_page_unknown_book(client, clean_db):
    assert client.get("/books/99999").status_code == 404


def test_edition_files_fragment(client, imported_book):
    edition = imported_book.editions[0]

    response = client.get(f"/editions/{edition.id}/files")

    assert response.status_code == 200
    assert "Part 01.mp3" in response.text
    assert "cover.jpg" in response.text
    assert "128 kb/s" in response.text  # mutagen bitrate for the mp3
    assert response.text.count("<td>?</td>") == 1  # no bitrate for the jpg


def test_edition_files_fragment_missing_folder(client, clean_db, book):
    edition = make_edition(clean_db, book, download_state=DownloadState.IMPORTED,
                           library_path="/audiobooks/gone")

    response = client.get(f"/editions/{edition.id}/files")

    assert response.status_code == 200
    assert "Folder not found" in response.text


def test_edition_files_fragment_unknown_edition(client, clean_db):
    assert client.get("/editions/99999/files").status_code == 404


@respx.mock
def test_replace_picker_bypasses_available_block(client, imported_book):
    mock_search()

    response = client.get(f"/books/{imported_book.id}/releases?replace=1")

    assert response.status_code == 200
    assert "already available" not in response.text
    assert 'value="after_import"' in response.text
    assert 'value="immediately"' in response.text
    assert '#replace-options' in response.text


@respx.mock
def test_replace_picker_still_blocks_downloading_book(client, clean_db, book):
    make_edition(clean_db, book, download_state=DownloadState.DOWNLOADING)

    response = client.get(f"/books/{book.id}/releases?replace=1")

    assert response.status_code == 200
    assert "already downloading" in response.text


def grab_data(**extra):
    return {"guid": GUID, "indexer": "AudioBookBay", "title": "Noobtown", "size": "", **extra}


@respx.mock
def test_replace_grab_immediately_removes_files(client, clean_db, imported_book):
    mock_details()
    mock_deluge()
    edition = imported_book.editions[0]
    lib = Path(edition.library_path)

    response = client.post(
        f"/books/{imported_book.id}/grab", data=grab_data(replace="1", remove="immediately")
    )

    assert response.status_code == 200
    assert '<div id="modal"></div>' in response.text
    assert "downloading" in response.text
    assert not lib.exists()
    clean_db.expire_all()
    assert edition.library_path is None
    assert edition.audio_files == []
    assert edition.download_state == DownloadState.GRABBED
    release = clean_db.query(Release).one()
    assert release.edition_id == edition.id
    assert get_state(clean_db, replace_key(release)) is None


# Downloading needs both DOWNLOAD_CLIENT and DOWNLOAD_URL; blanking either
# disables it, so every disabled-mode test runs in both configurations.
@pytest.fixture(params=["download_client", "download_url"])
def downloads_disabled(request, download_config, test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, request.param, "")


def test_no_download_button_when_downloads_disabled(client, book, downloads_disabled):
    response = client.get("/")

    assert response.status_code == 200
    assert "The Mayor of Noobtown" in response.text
    assert "download-btn" not in response.text


def test_release_picker_refuses_when_downloads_disabled(client, book, downloads_disabled):
    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert "not configured" in response.text
    assert "Grab" not in response.text


def test_grab_refuses_when_downloads_disabled(client, clean_db, book, downloads_disabled):
    response = client.post(f"/books/{book.id}/grab", data=grab_data())

    assert response.status_code == 409
    assert clean_db.query(Release).count() == 0


def test_detail_page_hides_replace_when_downloads_disabled(
    client, imported_book, downloads_disabled
):
    response = client.get(f"/books/{imported_book.id}")

    assert response.status_code == 200
    # path (and lazy file table) still shown
    assert imported_book.editions[0].library_path in response.text
    assert "replace=1" not in response.text
    assert "Download another edition" not in response.text


@respx.mock
def test_grab_from_detail_page_redirects_back(client, clean_db, imported_book):
    mock_details()
    mock_deluge()

    response = client.post(
        f"/books/{imported_book.id}/grab",
        data=grab_data(replace="1", from_detail="1"),
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/books/{imported_book.id}"
    assert clean_db.query(Release).count() == 1


@respx.mock
def test_replace_grab_deferred_keeps_files(client, clean_db, imported_book):
    mock_details()
    mock_deluge()
    edition = imported_book.editions[0]
    lib = Path(edition.library_path)

    response = client.post(f"/books/{imported_book.id}/grab", data=grab_data(replace="1"))

    assert response.status_code == 200
    assert (lib / "Part 01.mp3").exists()
    clean_db.expire_all()
    assert edition.library_path == str(lib)
    assert edition.download_state == DownloadState.GRABBED
    release = clean_db.query(Release).one()
    assert release.edition_id == edition.id
    assert get_state(clean_db, replace_key(release)) == "1"
