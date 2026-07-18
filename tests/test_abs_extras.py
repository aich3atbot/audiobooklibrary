"""Tests for the post-audit ABS additions: search, playlists/collections
shims, bookmarks, stats, and progress-fraction PATCH."""

import pytest

from app.models import Bookmark, MediaProgress
from tests.test_abs_catalogue import clean_db, get, library, token  # noqa: F401


@pytest.fixture(autouse=True)
def _clear_bookmarks(db_session):
    db_session.query(Bookmark).delete()
    db_session.commit()


def test_search_by_title(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/search", q="hail mary").json()
    assert len(body["book"]) == 1
    item = body["book"][0]["libraryItem"]
    assert item["media"]["metadata"]["title"] == "Project Hail Mary"
    assert item["media"]["audioFiles"]  # expanded item
    assert body["series"] == []


def test_search_by_series_and_author(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/search", q="noobtown").json()
    assert len(body["book"]) == 1
    assert len(body["series"]) == 1
    assert body["series"][0]["series"]["name"] == "Noobtown"
    assert len(body["series"][0]["books"]) == 1

    body = get(client, token, "/api/libraries/lib_audiobooks/search", q="rimmel").json()
    assert len(body["authors"]) == 1
    assert body["authors"][0]["name"] == "Ryan Rimmel"
    assert body["authors"][0]["numBooks"] == 1


def test_search_requires_query(client, token, library):
    assert get(client, token, "/api/libraries/lib_audiobooks/search").status_code == 400


def test_playlists_and_collections_empty(client, token, library):
    playlists = get(client, token, "/api/libraries/lib_audiobooks/playlists").json()
    assert playlists == {"results": [], "total": 0, "limit": 0, "page": 0}
    collections = get(client, token, "/api/libraries/lib_audiobooks/collections").json()
    assert collections["results"] == []
    assert collections["total"] == 0


def test_bookmark_crud_and_user_payload(client, token, library):
    item_id = f"li_{library['mayor'].id}"
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(f"/api/me/item/{item_id}/bookmark",
                          json={"time": 123.5, "title": "Great scene"}, headers=headers)
    assert created.status_code == 200
    body = created.json()
    assert body["libraryItemId"] == item_id
    assert body["time"] == 123.5
    assert body["createdAt"]

    me = get(client, token, "/api/me").json()
    assert len(me["bookmarks"]) == 1
    assert me["bookmarks"][0]["title"] == "Great scene"

    updated = client.patch(f"/api/me/item/{item_id}/bookmark",
                           json={"time": 123.5, "title": "Renamed"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["title"] == "Renamed"

    deleted = client.delete(f"/api/me/item/{item_id}/bookmark/123.5", headers=headers)
    assert deleted.status_code == 200
    assert get(client, token, "/api/me").json()["bookmarks"] == []


def test_bookmark_validation(client, token, library):
    item_id = f"li_{library['mayor'].id}"
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(f"/api/me/item/{item_id}/bookmark",
                       json={"time": 5}, headers=headers).status_code == 400
    assert client.patch(f"/api/me/item/{item_id}/bookmark",
                        json={"time": 999, "title": "x"}, headers=headers).status_code == 404
    assert client.delete(f"/api/me/item/{item_id}/bookmark/999",
                         headers=headers).status_code == 404


def test_listening_stats_zeroed(client, token, library):
    body = get(client, token, "/api/me/listening-stats").json()
    assert body["totalTime"] == 0
    assert body["recentSessions"] == []


def test_year_stats_counts_finished(client, token, library, user):
    from datetime import datetime

    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=100.0, duration=100.0,
                         is_finished=True, finished_at=datetime(2026, 3, 1)))
    db.commit()

    body = get(client, token, "/api/me/stats/year/2026").json()
    assert body["numBooksFinished"] == 1
    assert get(client, token, "/api/me/stats/year/2020").json()["numBooksFinished"] == 0


def test_patch_progress_fraction_maps_to_current_time(client, token, library,
                                                      tokenless_user):
    db = library["db"]
    item_id = f"li_{library['hail'].id}"

    response = client.patch(f"/api/me/progress/{item_id}",
                            json={"progress": 0.5, "duration": 200.0},
                            headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=library["hail"].id).one()
    assert progress.current_time == pytest.approx(100.0)
    assert progress.is_finished is False
