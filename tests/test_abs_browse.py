"""ABS browse surface used by third-party clients (Lissen): library-item
filters, the author landing page, and batch item fetch."""

import base64

import pytest

from app.models import Book, DownloadState, Edition, MediaProgress
from tests.test_abs_catalogue import clean_db, get, library, token  # noqa: F401
from tests.test_audio_meta import write_mp3


def encode(value: str) -> str:
    """Clients send filter values base64-encoded; padding is optional."""
    return base64.b64encode(value.encode()).decode().rstrip("=")


@pytest.fixture
def series_library(library, test_settings):
    """The shared fixture plus a second Noobtown book, so series filtering and
    the sequence sort have something to order."""
    db = library["db"]
    mayor_book = library["mayor"].book
    ranger_dir = test_settings.library_dir / "Ryan Rimmel" / "Noobtown" / "2 - The Ranger of Noobtown"
    write_mp3(ranger_dir / "Ranger.mp3", frames=100)
    ranger_book = Book(
        hardcover_id=646490, title="The Ranger of Noobtown", author=mayor_book.author,
        series=mayor_book.series, series_index=2.0,
    )
    ranger = Edition(
        book=ranger_book, download_state=DownloadState.IMPORTED, library_path=str(ranger_dir)
    )
    db.add(ranger)
    db.commit()

    from app.services.audio_meta import scan_edition_audio

    scan_edition_audio(db, ranger)
    return {**library, "ranger": ranger}


def items(client, token, **params):
    return get(client, token, "/api/libraries/lib_audiobooks/items", **params).json()


def test_series_filter_and_sequence_sort(client, token, series_library):
    series_id = f"ser_{series_library['mayor'].book.series_id}"
    body = items(client, token, filter=f"series.{encode(series_id)}", sort="sequence")

    assert body["total"] == 2
    titles = [r["media"]["metadata"]["title"] for r in body["results"]]
    assert titles == ["The Mayor of Noobtown", "The Ranger of Noobtown"]
    assert body["filterBy"] == f"series.{encode(series_id)}"

    desc = items(client, token, filter=f"series.{encode(series_id)}", sort="sequence", desc="1")
    assert [r["media"]["metadata"]["title"] for r in desc["results"]] == [
        "The Ranger of Noobtown", "The Mayor of Noobtown",
    ]


def test_sequence_sort_ignored_without_series_filter(client, token, series_library):
    """ABS drops a sequence sort outside a series filter — books with no series
    would otherwise all collide on one key."""
    body = items(client, token, sort="sequence")
    assert body["total"] == 3
    titles = [r["media"]["metadata"]["title"] for r in body["results"]]
    assert titles == sorted(titles)


def test_author_filter(client, token, library):
    author_id = f"aut_{library['hail'].book.author_id}"
    body = items(client, token, filter=f"authors.{encode(author_id)}")
    assert [r["media"]["metadata"]["title"] for r in body["results"]] == ["Project Hail Mary"]


def test_progress_filters(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=30.0, duration=100.0))
    db.add(MediaProgress(user_id=user.id, edition_id=library["hail"].id,
                         current_time=100.0, duration=100.0, is_finished=True))
    db.commit()

    def titles(value):
        body = items(client, token, filter=f"progress.{encode(value)}")
        return sorted(r["media"]["metadata"]["title"] for r in body["results"])

    assert titles("in-progress") == ["The Mayor of Noobtown"]
    assert titles("finished") == ["Project Hail Mary"]
    assert titles("not-finished") == ["The Mayor of Noobtown"]
    assert titles("not-started") == []


def test_progress_filter_is_per_user(client, token, library, user, db_session):
    """One user's progress must not filter another's library view."""
    from app.models import User
    from tests.conftest import cheap_password_hash

    other = db_session.query(User).filter_by(username="pat").one_or_none()
    if other is None:
        other = User(username="pat", password_hash=cheap_password_hash())
        db_session.add(other)
        db_session.commit()
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=30.0, duration=100.0))
    db.commit()

    other_token = client.post(
        "/login", json={"username": "pat", "password": "hunter2"},
        headers={"x-return-tokens": "true"},
    ).json()["user"]["accessToken"]

    assert items(client, token, filter=f"progress.{encode('in-progress')}")["total"] == 1
    assert items(client, other_token, filter=f"progress.{encode('in-progress')}")["total"] == 0


def test_filter_group_we_hold_no_data_for_is_empty(client, token, library):
    body = items(client, token, filter=f"genres.{encode('Fantasy')}")
    assert body["results"] == []
    assert body["total"] == 0


def test_author_items(client, token, series_library):
    author_id = f"aut_{series_library['mayor'].book.author_id}"
    body = get(client, token, f"/api/authors/{author_id}", include="items,series").json()

    assert body["id"] == author_id
    assert body["name"] == "Ryan Rimmel"
    assert body["imagePath"] is None
    assert [i["media"]["metadata"]["title"] for i in body["libraryItems"]] == [
        "The Mayor of Noobtown", "The Ranger of Noobtown",
    ]
    series = body["series"][0]
    assert series["name"] == "Noobtown"
    assert [i["media"]["metadata"]["series"]["sequence"] for i in series["items"]] == ["1", "2"]


def test_author_without_include_has_no_items(client, token, library):
    author_id = f"aut_{library['hail'].book.author_id}"
    body = get(client, token, f"/api/authors/{author_id}").json()
    assert "libraryItems" not in body


def test_author_unknown_404(client, token, library):
    assert get(client, token, "/api/authors/aut_9999").status_code == 404


def test_batch_get_items(client, token, library):
    mayor = f"li_{library['mayor'].id}"
    hail = f"li_{library['hail'].id}"
    response = client.post(
        "/api/items/batch/get",
        json={"libraryItemIds": [hail, mayor, "li_99999"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [i["id"] for i in body["libraryItems"]] == [hail, mayor]
    assert body["libraryItems"][1]["media"]["tracks"]


def test_batch_get_empty_payload_403(client, token, library):
    response = client.post(
        "/api/items/batch/get",
        json={"libraryItemIds": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
