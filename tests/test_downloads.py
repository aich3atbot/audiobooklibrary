import json

import httpx
import pytest
import respx

from app.models import AppState, Author, Book, DownloadState, ReadState, Release, Series
from app.services.downloads import grab_release, search_releases

PROWLARR = "http://host.docker.internal:9696"


@pytest.fixture
def clean_db(db_session):
    for model in (Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def book(clean_db):
    author = Author(hardcover_id=500, name="Ryan Rimmel")
    book = Book(
        hardcover_id=646489,
        title="The Mayor of Noobtown",
        author=author,
        read_state=ReadState.WANT_TO_READ,
    )
    clean_db.add(book)
    clean_db.commit()
    return book


def release(guid="guid-1", seeders=5, title="The Mayor of Noobtown [M4B]", size=133567608):
    return {
        "guid": guid,
        "indexerId": 1,
        "indexer": "AudioBook Bay",
        "title": title,
        "size": size,
        "seeders": seeders,
        "protocol": "torrent",
    }


@respx.mock
def test_search_releases_sorted_by_seeders(book):
    route = respx.get(f"{PROWLARR}/api/v1/search").mock(
        return_value=httpx.Response(
            200, json=[release(guid="a", seeders=1), release(guid="b", seeders=9)]
        )
    )

    results = search_releases(book)

    assert [r["guid"] for r in results] == ["b", "a"]
    params = route.calls[0].request.url.params
    assert params["query"] == "Ryan Rimmel The Mayor of Noobtown"
    assert params["categories"] == "3030"


@respx.mock
def test_search_releases_falls_back_to_title_only(book):
    route = respx.get(f"{PROWLARR}/api/v1/search")
    route.side_effect = [
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[release()]),
    ]

    results = search_releases(book)

    assert len(results) == 1
    assert route.calls[1].request.url.params["query"] == "The Mayor of Noobtown"


@respx.mock
def test_grab_release_records_and_updates_state(clean_db, book):
    route = respx.post(f"{PROWLARR}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )

    grab_release(clean_db, book, "guid-1", 1, "The Mayor of Noobtown [M4B]", 133567608, 5)

    assert json.loads(route.calls[0].request.content) == {"guid": "guid-1", "indexerId": 1}
    assert book.download_state == DownloadState.GRABBED
    stored = clean_db.query(Release).one()
    assert stored.prowlarr_guid == "guid-1"
    assert stored.status == "grabbed"
    assert stored.grabbed_at is not None


@respx.mock
def test_grab_failure_leaves_state_untouched(clean_db, book):
    respx.post(f"{PROWLARR}/api/v1/search").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(Exception):
        grab_release(clean_db, book, "guid-1", 1, "title", None, None)

    assert book.download_state == DownloadState.NONE
    assert clean_db.query(Release).count() == 0


@respx.mock
def test_releases_route_renders_dialog(client, book):
    respx.get(f"{PROWLARR}/api/v1/search").mock(
        return_value=httpx.Response(200, json=[release(seeders=3)])
    )

    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert "best match" in response.text
    assert "127.4 MB" in response.text
    assert f"/books/{book.id}/grab" in response.text


@respx.mock
def test_grab_route_closes_modal_and_updates_card(client, clean_db, book):
    respx.post(f"{PROWLARR}/api/v1/search").mock(return_value=httpx.Response(200, json={}))

    response = client.post(
        f"/books/{book.id}/grab",
        data={
            "guid": "guid-1",
            "indexer_id": "1",
            "title": "The Mayor of Noobtown [M4B]",
            "size": "133567608",
            "seeders": "5",
        },
    )

    assert response.status_code == 200
    assert '<div id="modal"></div>' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert "grabbed" in response.text


@respx.mock
def test_grab_route_failure_shows_error(client, clean_db, book):
    respx.post(f"{PROWLARR}/api/v1/search").mock(return_value=httpx.Response(500, text="boom"))

    response = client.post(
        f"/books/{book.id}/grab",
        data={"guid": "guid-1", "indexer_id": "1", "title": "x"},
    )

    assert response.status_code == 200
    assert "Grab failed" in response.text
    assert clean_db.query(Release).count() == 0
