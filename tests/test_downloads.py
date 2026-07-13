import json
from pathlib import Path

import httpx
import pytest
import respx

from app.models import (
    AppState,
    Author,
    Book,
    DownloadState,
    Release,
    Series,
    UserBook,
)
from app.services.downloads import grab_release, search_releases
from tests.conftest import make_user_book

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
    for model in (UserBook, Release, Book, Author, Series, AppState):
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
        download_state=DownloadState.NONE,
    )
    clean_db.add(book)
    clean_db.commit()
    make_user_book(clean_db, user, book)
    return book


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
    assert book.download_state == DownloadState.GRABBED

    add = json.loads(deluge.calls[-1].request.content)
    assert add["method"] == "core.add_torrent_magnet"
    assert add["params"][0] == release.magnet_uri


@respx.mock
def test_grab_release_survives_a_torrent_deluge_already_has(clean_db, book):
    mock_details()
    mock_deluge(add_error="Torrent already in session")

    release = grab_release(clean_db, None, book, GUID, "AudioBookBay", "Noobtown", None)

    assert release.info_hash == HASH  # recovered from the magnet
    assert book.download_state == DownloadState.GRABBED


@respx.mock
def test_grab_failure_leaves_state_untouched(clean_db, book):
    mock_details()
    mock_deluge(add_error="Invalid magnet")

    with pytest.raises(Exception):
        grab_release(clean_db, None, book, GUID, "AudioBookBay", "Noobtown", None)

    clean_db.rollback()
    assert clean_db.query(Release).count() == 0
    assert book.download_state == DownloadState.NONE


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
    book.download_state = DownloadState.IMPORTED
    book.library_path = "/audiobooks/x"
    clean_db.commit()

    response = client.post(
        f"/books/{book.id}/grab",
        data={"guid": GUID, "indexer": "AudioBookBay", "title": "Noobtown", "size": ""},
    )

    assert response.status_code == 409
    assert clean_db.query(Release).count() == 0


@respx.mock
def test_grab_blocked_when_book_downloading(client, clean_db, book):
    book.download_state = DownloadState.DOWNLOADING
    clean_db.commit()

    response = client.post(
        f"/books/{book.id}/grab",
        data={"guid": GUID, "indexer": "AudioBookBay", "title": "Noobtown", "size": ""},
    )

    assert response.status_code == 409


def test_release_picker_refuses_available_book(client, clean_db, book):
    book.download_state = DownloadState.IMPORTED
    clean_db.commit()

    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert "already available" in response.text
