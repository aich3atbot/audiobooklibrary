import json

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL
from app.models import (
    AppState,
    Author,
    Book,
    DownloadState,
    ReadState,
    Release,
    Series,
    UserBook,
)
from tests.conftest import make_user_book

ABB = "http://abb.test"
DELUGE = "http://deluge.test:8112"


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def download_config(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "index_url", ABB)
    monkeypatch.setattr(test_settings, "download_url", DELUGE)
    monkeypatch.setattr(test_settings, "download_client", "deluge")
    monkeypatch.setattr(test_settings, "download_password", "")


@pytest.fixture
def library(clean_db, user):
    sanderson = Author(hardcover_id=1, name="Brandon Sanderson")
    rimmel = Author(hardcover_id=2, name="Ryan Rimmel")
    noobtown = Series(hardcover_id=10, name="Noobtown")
    kings = Book(hardcover_id=100, title="The Way of Kings", author=sanderson)
    mayor = Book(hardcover_id=101, title="The Mayor of Noobtown", author=rimmel,
                 series=noobtown, series_index=1,
                 download_state=DownloadState.IMPORTED)
    village = Book(hardcover_id=102, title="Village of Noobtown", author=rimmel,
                   series=noobtown, series_index=2)
    clean_db.add_all([kings, mayor, village])
    clean_db.commit()
    make_user_book(clean_db, user, kings, read_state=ReadState.READ)
    make_user_book(clean_db, user, mayor, read_state=ReadState.WANT_TO_READ)
    make_user_book(clean_db, user, village, read_state=ReadState.READING)


def titles(response):
    return [t.replace("<h4>", "") for t in
            __import__("re").findall(r"<h4>[^<]+", response.text)]


def test_filter_by_text_matches_series(client, library):
    response = client.get("/", params={"q": "noobtown"})
    assert sorted(titles(response)) == ["The Mayor of Noobtown", "Village of Noobtown"]


def test_filter_by_author_name(client, library):
    response = client.get("/", params={"q": "sanderson"})
    assert titles(response) == ["The Way of Kings"]


def test_filter_by_read_state(client, library):
    response = client.get("/", params={"read": "reading"})
    assert titles(response) == ["Village of Noobtown"]


def test_filter_by_download_status(client, library):
    response = client.get("/", params={"dl": "available"})
    assert titles(response) == ["The Mayor of Noobtown"]

    response = client.get("/", params={"dl": "not_present"})
    assert sorted(titles(response)) == ["The Way of Kings", "Village of Noobtown"]


def test_library_only_shows_own_books(client, library, db_session, clean_db):
    """Books other users shelved (or ownerless available books) stay out of
    the library page."""
    ghost = Book(hardcover_id=999, title="Someone Else's Book",
                 author=db_session.query(Author).first(),
                 download_state=DownloadState.IMPORTED, library_path="/audiobooks/g")
    db_session.add(ghost)
    db_session.commit()

    response = client.get("/")
    assert "Someone Else's Book" not in response.text
    assert len(titles(response)) == 3


def test_sort_by_author(client, library):
    response = client.get("/", params={"sort": "author"})
    assert titles(response) == [
        "The Way of Kings", "The Mayor of Noobtown", "Village of Noobtown",
    ]


def test_bad_filter_values_ignored(client, library):
    response = client.get("/", params={"read": "bogus", "sort": "bogus", "dl": "bogus"})
    assert len(titles(response)) == 3


def deluge_ok(request):
    method = json.loads(request.content)["method"]
    results = {"auth.login": True, "web.connected": True, "daemon.get_version": "2.2.0"}
    return httpx.Response(200, json={"result": results[method], "error": None})


@respx.mock
def test_settings_page_all_connected(client, clean_db, download_config):
    respx.post(API_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"me": [{"id": 1, "username": "davidr"}]}}
        )
    )
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.post(f"{DELUGE}/json").mock(side_effect=deluge_ok)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "connected as davidr" in response.text
    assert "reachable" in response.text
    assert "Deluge daemon 2.2.0" in response.text
    assert response.text.count(">ok</span>") == 3


@respx.mock
def test_settings_page_shows_errors(client, clean_db, download_config):
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(503))
    respx.post(f"{DELUGE}/json").mock(side_effect=httpx.ConnectError("down"))

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.text.count(">error</span>") == 3


def test_settings_page_reports_unset_connections(
    client, clean_db, tokenless_user, test_settings, monkeypatch
):
    monkeypatch.setattr(test_settings, "index_url", "")
    monkeypatch.setattr(test_settings, "download_url", "")

    response = client.get("/settings")

    assert "no Hardcover token set" in response.text
    assert "INDEX_URL not set" in response.text
    assert "DOWNLOAD_URL not set" in response.text
