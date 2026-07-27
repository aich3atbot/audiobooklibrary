import shutil

import pytest

from app.models import (
    AppState,
    AudioFile,
    Author,
    Book,
    Bookmark,
    DownloadState,
    Edition,
    MediaProgress,
    Release,
    Series,
    UserBook,
)
from tests.test_audio_meta import write_mp3


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, AudioFile, MediaProgress, Bookmark, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def token(client):
    from tests.conftest import TEST_PASSWORD, TEST_USERNAME

    response = client.post(
        "/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        headers={"x-return-tokens": "true"},
    )
    return response.json()["user"]["accessToken"]


@pytest.fixture
def library(clean_db, test_settings):
    """Two imported books (one with series + audio files + cover) and one
    unimported book that must stay invisible to the API."""
    lib_root = test_settings.library_dir
    if lib_root.exists():
        shutil.rmtree(lib_root)

    rimmel = Author(hardcover_id=500, name="Ryan Rimmel")
    noobtown = Series(hardcover_id=300, name="Noobtown")
    weir = Author(hardcover_id=501, name="Andy Weir")

    mayor_dir = lib_root / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    write_mp3(mayor_dir / "Part 1.mp3", frames=100)
    write_mp3(mayor_dir / "Part 2.mp3", frames=100)
    (mayor_dir / "cover.jpg").write_bytes(b"cover-bytes")
    mayor_book = Book(
        hardcover_id=646489, title="The Mayor of Noobtown", author=rimmel,
        series=noobtown, series_index=1.0,
        cover_url="https://assets.hardcover.app/mayor.jpg",
    )
    mayor = Edition(
        book=mayor_book, download_state=DownloadState.IMPORTED, library_path=str(mayor_dir)
    )

    hail_dir = lib_root / "Andy Weir" / "Project Hail Mary"
    write_mp3(hail_dir / "Project Hail Mary.mp3", frames=200)
    hail_book = Book(
        hardcover_id=700, title="Project Hail Mary", author=weir,
        cover_url="https://assets.hardcover.app/phm.jpg",
    )
    hail = Edition(
        book=hail_book, download_state=DownloadState.IMPORTED, library_path=str(hail_dir)
    )

    unimported = Book(
        hardcover_id=800, title="Not Downloaded", author=weir,
    )

    clean_db.add_all([mayor, hail, unimported])
    clean_db.commit()

    from app.services.audio_meta import scan_edition_audio

    scan_edition_audio(clean_db, mayor)
    scan_edition_audio(clean_db, hail)
    # "mayor"/"hail" are the *editions* (the API's item unit); .book has metadata
    return {"mayor": mayor, "hail": hail, "unimported": unimported, "db": clean_db}


def get(client, token, path, **params):
    return client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})


def test_libraries_list(client, token):
    body = get(client, token, "/api/libraries").json()
    assert len(body["libraries"]) == 1
    lib = body["libraries"][0]
    assert lib["id"] == "lib_audiobooks"
    assert lib["mediaType"] == "book"
    assert lib["folders"][0]["fullPath"]


def test_items_only_imported_books(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/items").json()
    assert body["total"] == 2
    titles = [r["media"]["metadata"]["title"] for r in body["results"]]
    assert "Not Downloaded" not in titles
    mayor = next(r for r in body["results"] if r["media"]["metadata"]["title"] == "The Mayor of Noobtown")
    assert mayor["id"] == f"li_{library['mayor'].id}"
    assert mayor["media"]["numTracks"] == 2
    assert mayor["media"]["metadata"]["seriesName"] == "Noobtown #1"
    assert mayor["media"]["metadata"]["authorNameLF"] == "Rimmel, Ryan"
    assert mayor["media"]["duration"] > 0


def test_items_sort_and_pagination(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/items",
               sort="media.metadata.authorName", limit=1, page=1).json()
    assert body["total"] == 2
    assert len(body["results"]) == 1
    assert body["results"][0]["media"]["metadata"]["authorName"] == "Ryan Rimmel"


def test_item_expanded(client, token, library):
    item_id = f"li_{library['mayor'].id}"
    body = get(client, token, f"/api/items/{item_id}", expanded=1, include="progress").json()
    media = body["media"]
    assert len(media["audioFiles"]) == 2
    assert media["audioFiles"][0]["mimeType"] == "audio/mpeg"
    assert len(media["tracks"]) == 2
    track2 = media["tracks"][1]
    assert track2["startOffset"] == pytest.approx(media["tracks"][0]["duration"], rel=0.01)
    assert track2["contentUrl"].startswith(f"/api/items/{item_id}/file/")
    assert len(media["chapters"]) == 2
    assert media["chapters"][0]["title"] == "Part 1"
    assert body["userMediaProgress"] is None
    assert media["metadata"]["series"][0]["sequence"] == "1"


def test_item_detail_without_expanded_is_the_full_item(client, token, library):
    """ABS returns the *full* item (not the minified list shape) when a client
    omits expanded=1 — Lissen does, and builds its chapter list from these."""
    item_id = f"li_{library['mayor'].id}"
    body = get(client, token, f"/api/items/{item_id}").json()
    media = body["media"]

    assert len(media["chapters"]) == 2
    assert media["chapters"][0]["title"] == "Part 1"
    assert len(media["audioFiles"]) == 2
    assert media["libraryItemId"] == item_id
    assert media["ebookFile"] is None
    assert body["libraryFiles"] == []
    # expanded metadata, not the minified block
    assert media["metadata"]["authors"][0]["name"] == "Ryan Rimmel"
    assert media["metadata"]["narrators"] == []
    assert media["metadata"]["series"][0]["sequence"] == "1"
    # playable tracks are expanded-only upstream
    assert "tracks" not in media

    # the fields clients deserialize as non-null (Lissen's Moshi adapters)
    assert body["ino"] and body["libraryId"] and body["addedAt"] and body["ctimeMs"]
    audio = media["audioFiles"][0]
    assert audio["ino"] and audio["mimeType"] == "audio/mpeg"
    assert audio["metadata"]["filename"] and audio["metadata"]["ext"]
    assert audio["metadata"]["size"] > 0


def test_chapters_merge_embedded_across_files(client, token, library):
    """A file's own chapters are shifted by where the file starts; files
    without any still contribute one chapter each."""
    import json

    db = library["db"]
    mayor = library["mayor"]
    first, second = mayor.audio_files
    first.chapters_json = json.dumps([
        {"id": 0, "start": 0.0, "end": 1.0, "title": "Opening"},
        {"id": 1, "start": 1.0, "end": None, "title": "Middle"},
    ])
    db.commit()

    chapters = get(client, token, f"/api/items/li_{mayor.id}").json()["media"]["chapters"]

    assert [c["title"] for c in chapters] == ["Opening", "Middle", "Part 2"]
    assert [c["id"] for c in chapters] == [0, 1, 2]
    # the open-ended chapter is closed with its own track's length
    assert chapters[1]["end"] == pytest.approx(first.duration)
    assert chapters[2]["start"] == pytest.approx(first.duration)
    assert chapters[2]["end"] == pytest.approx(first.duration + second.duration)


def test_item_detail_progress_include(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=10.0, duration=100.0))
    db.commit()
    body = get(client, token, f"/api/items/li_{library['mayor'].id}",
               include="progress").json()
    assert body["userMediaProgress"]["currentTime"] == 10.0


def test_item_not_found(client, token, library):
    assert get(client, token, "/api/items/li_99999").status_code == 404
    assert get(client, token, "/api/items/garbage").status_code == 404


def test_cover_local_file(client, library):
    """Covers are unauthenticated (as upstream): since server 2.17.0 the
    official apps send no token with cover requests at all."""
    item_id = f"li_{library['mayor'].id}"
    response = client.get(f"/api/items/{item_id}/cover")
    assert response.status_code == 200
    assert response.content == b"cover-bytes"


def test_cover_redirects_to_hardcover(client, library):
    item_id = f"li_{library['hail'].id}"
    response = client.get(f"/api/items/{item_id}/cover", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://assets.hardcover.app/phm.jpg"


def test_cover_unknown_item_404(client, library):
    assert client.get("/api/items/li_99999/cover").status_code == 404


def test_personalized_shelves(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id, current_time=30.0,
                         duration=100.0, is_finished=False))
    db.commit()

    shelves = get(client, token, "/api/libraries/lib_audiobooks/personalized").json()
    ids = [s["id"] for s in shelves]
    assert ids[0] == "continue-listening"
    assert "recently-added" in ids
    continue_shelf = shelves[0]
    assert continue_shelf["entities"][0]["media"]["metadata"]["title"] == "The Mayor of Noobtown"


def test_filterdata(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/filterdata").json()
    assert {"id": f"ser_{library['mayor'].book.series_id}", "name": "Noobtown"} in body["series"]
    assert len(body["authors"]) == 2


def test_series_endpoint(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/series").json()
    assert body["total"] == 1
    assert body["results"][0]["name"] == "Noobtown"
    assert len(body["results"][0]["books"]) == 1


def test_authors_endpoint(client, token, library):
    body = get(client, token, "/api/libraries/lib_audiobooks/authors").json()
    names = [a["name"] for a in body["authors"]]
    assert names == ["Andy Weir", "Ryan Rimmel"]
    rimmel = body["authors"][1]
    assert rimmel["numBooks"] == 1
    assert rimmel["lastFirst"] == "Rimmel, Ryan"


def test_me_includes_progress(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["hail"].id, current_time=10.0,
                         duration=50.0, is_finished=False))
    db.commit()

    body = get(client, token, "/api/me").json()
    assert len(body["mediaProgress"]) == 1
    progress = body["mediaProgress"][0]
    assert progress["libraryItemId"] == f"li_{library['hail'].id}"
    assert progress["progress"] == pytest.approx(0.2)
    assert progress["mediaItemType"] == "book"


def test_unknown_library_404(client, token):
    assert get(client, token, "/api/libraries/lib_other/items").status_code == 404


def test_multi_edition_items_carry_their_label(client, token, library, test_settings):
    """Two editions of one book are two items; titles disambiguate them."""
    db = library["db"]
    mayor = library["mayor"]
    mayor.label = "Jim Dale"
    fry_dir = (test_settings.library_dir / "Ryan Rimmel" / "Noobtown {Stephen Fry}"
               / "1 - The Mayor of Noobtown")
    write_mp3(fry_dir / "fry.mp3", frames=50)
    fry = Edition(book=mayor.book, label="Stephen Fry",
                  download_state=DownloadState.IMPORTED, library_path=str(fry_dir))
    db.add(fry)
    db.commit()
    from app.services.audio_meta import scan_edition_audio

    scan_edition_audio(db, fry)

    body = get(client, token, "/api/libraries/lib_audiobooks/items").json()
    titles = sorted(r["media"]["metadata"]["title"] for r in body["results"])
    assert "The Mayor of Noobtown (Jim Dale)" in titles
    assert "The Mayor of Noobtown (Stephen Fry)" in titles
    # the single-edition book keeps its plain title
    assert "Project Hail Mary" in titles

    item = get(client, token, f"/api/items/li_{fry.id}", expanded=1).json()
    assert item["media"]["metadata"]["narrators"] == ["Stephen Fry"]

    filters = get(client, token, "/api/libraries/lib_audiobooks/filterdata").json()
    assert "Stephen Fry" in filters["narrators"]
    assert "Jim Dale" in filters["narrators"]
