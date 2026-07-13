import httpx
import pytest
import respx

from app.clients.hardcover import API_URL
from app.models import AppState, Author, Book, DownloadState, ReadState, Release, Series

PROWLARR = "http://host.docker.internal:9696"


@pytest.fixture
def clean_db(db_session):
    for model in (Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def hardcover_token(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings.__class__, "hardcover_token", "token", raising=False)


@pytest.fixture
def prowlarr_key(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings.__class__, "prowlarr_api_key", "key", raising=False)


@pytest.fixture
def library(clean_db):
    sanderson = Author(hardcover_id=1, name="Brandon Sanderson")
    rimmel = Author(hardcover_id=2, name="Ryan Rimmel")
    noobtown = Series(hardcover_id=10, name="Noobtown")
    clean_db.add_all(
        [
            Book(hardcover_id=100, title="The Way of Kings", author=sanderson,
                 read_state=ReadState.READ),
            Book(hardcover_id=101, title="The Mayor of Noobtown", author=rimmel,
                 series=noobtown, series_index=1,
                 read_state=ReadState.WANT_TO_READ,
                 download_state=DownloadState.IMPORTED),
            Book(hardcover_id=102, title="Village of Noobtown", author=rimmel,
                 series=noobtown, series_index=2,
                 read_state=ReadState.READING),
        ]
    )
    clean_db.commit()


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


def test_filter_by_download_state(client, library):
    response = client.get("/", params={"dl": "imported"})
    assert titles(response) == ["The Mayor of Noobtown"]


def test_sort_by_author(client, library):
    response = client.get("/", params={"sort": "author"})
    assert titles(response) == [
        "The Way of Kings", "The Mayor of Noobtown", "Village of Noobtown",
    ]


def test_bad_filter_values_ignored(client, library):
    response = client.get("/", params={"read": "bogus", "sort": "bogus", "dl": "bogus"})
    assert len(titles(response)) == 3


@respx.mock
def test_settings_page_all_connected(client, clean_db, hardcover_token, prowlarr_key):
    respx.post(API_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"me": [{"id": 1, "username": "davidr"}]}}
        )
    )
    respx.get(f"{PROWLARR}/api/v1/system/status").mock(
        return_value=httpx.Response(200, json={"appName": "Prowlarr", "version": "10.0.0"})
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "connected as davidr" in response.text
    assert "Prowlarr 10.0.0" in response.text
    assert response.text.count(">ok</span>") == 2


@respx.mock
def test_settings_page_shows_errors(client, clean_db, hardcover_token, prowlarr_key):
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{PROWLARR}/api/v1/system/status").mock(return_value=httpx.Response(401))

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.text.count(">error</span>") == 2
