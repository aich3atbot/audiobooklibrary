import httpx
import pytest
import respx

from app.abs.playback_routes import open_sessions
from app.clients.hardcover import API_URL
from app.models import MediaProgress, ReadState, UserBook
from tests.conftest import make_user_book
from tests.test_abs_catalogue import clean_db, get, library, token  # noqa: F401


@pytest.fixture(autouse=True)
def _clear_sessions():
    open_sessions.clear()
    yield
    open_sessions.clear()


def post(client, tok, path, json=None):
    return client.post(path, json=json or {}, headers={"Authorization": f"Bearer {tok}"})


def play(client, tok, item_id):
    return post(
        client, tok, f"/api/items/{item_id}/play",
        json={
            "deviceInfo": {"deviceId": "dev-1", "clientName": "Abs Android",
                           "clientVersion": "0.10.0", "manufacturer": "Google",
                           "model": "Pixel"},
            "mediaPlayer": "exo-player",
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4"],
            "forceDirectPlay": False,
        },
    )


def test_play_creates_direct_session(client, token, library):
    item_id = f"li_{library['mayor'].id}"
    body = play(client, token, item_id).json()

    assert body["playMethod"] == 0
    assert body["libraryItemId"] == item_id
    assert body["startTime"] == 0.0
    assert body["mediaPlayer"] == "exo-player"
    assert body["deviceInfo"]["id"] == "dev-1"
    assert len(body["audioTracks"]) == 2
    track = body["audioTracks"][0]
    assert track["contentUrl"].startswith(f"/api/items/{item_id}/file/")
    assert body["libraryItem"]["media"]["tracks"]
    assert body["id"] in open_sessions


def test_play_resumes_from_progress(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=42.0, duration=100.0))
    db.commit()

    body = play(client, token, f"li_{library['mayor'].id}").json()
    assert body["startTime"] == 42.0


def test_play_restarts_finished_book(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id, current_time=100.0,
                         duration=100.0, is_finished=True))
    db.commit()

    body = play(client, token, f"li_{library['mayor'].id}").json()
    assert body["startTime"] == 0.0


def test_stream_file_with_range(client, token, library):
    item_id = f"li_{library['mayor'].id}"
    expanded = get(client, token, f"/api/items/{item_id}", expanded=1).json()
    ino = expanded["media"]["audioFiles"][0]["ino"]

    full = client.get(f"/api/items/{item_id}/file/{ino}", params={"token": token})
    assert full.status_code == 200
    assert full.headers["content-type"] == "audio/mpeg"

    partial = client.get(
        f"/api/items/{item_id}/file/{ino}",
        params={"token": token},
        headers={"Range": "bytes=0-99"},
    )
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert partial.headers["content-range"].startswith("bytes 0-99/")


def test_public_session_track_streams_without_token(client, token, library):
    """Direct play (server >= 2.22.0) streams from /public/session/:id/track/:index
    with no credential but the session id — and must not be redirected to /login."""
    session_id = play(client, token, f"li_{library['mayor'].id}").json()["id"]

    full = client.get(f"/public/session/{session_id}/track/1", follow_redirects=False)
    assert full.status_code == 200
    assert full.headers["content-type"] == "audio/mpeg"

    partial = client.get(
        f"/public/session/{session_id}/track/2", headers={"Range": "bytes=0-99"}
    )
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert partial.headers["content-range"].startswith("bytes 0-99/")


def test_public_session_track_unknown_session_or_index(client, token, library):
    session_id = play(client, token, f"li_{library['mayor'].id}").json()["id"]
    assert client.get(f"/public/session/{session_id}/track/9").status_code == 404
    assert client.get("/public/session/play_missing/track/1").status_code == 404


def test_play_replaces_previous_session_for_device(client, token, library):
    first = play(client, token, f"li_{library['mayor'].id}").json()["id"]
    second = play(client, token, f"li_{library['hail'].id}").json()["id"]
    assert first not in open_sessions
    assert second in open_sessions


def test_download_file_attachment(client, token, library):
    item_id = f"li_{library['mayor'].id}"
    expanded = get(client, token, f"/api/items/{item_id}", expanded=1).json()
    ino = expanded["media"]["audioFiles"][0]["ino"]

    response = get(client, token, f"/api/items/{item_id}/file/{ino}/download")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "Part%201.mp3" in disposition or 'filename="Part 1.mp3"' in disposition


def test_file_wrong_book_404(client, token, library):
    other_item = f"li_{library['hail'].id}"
    mayor_expanded = get(client, token, f"/api/items/li_{library['mayor'].id}",
                         expanded=1).json()
    ino = mayor_expanded["media"]["audioFiles"][0]["ino"]
    assert get(client, token, f"/api/items/{other_item}/file/{ino}").status_code == 404


def test_session_sync_updates_progress(client, token, library):
    db = library["db"]
    session_id = play(client, token, f"li_{library['mayor'].id}").json()["id"]

    response = post(client, token, f"/api/session/{session_id}/sync",
                    json={"currentTime": 30.0, "timeListened": 15, "duration": 100.0})
    assert response.status_code == 200

    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=library["mayor"].id).one()
    assert progress.current_time == 30.0
    assert progress.is_finished is False


@respx.mock
def test_session_sync_finish_marks_hardcover_read(client, token, library, user):
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"update_user_book": {"id": 42, "error": None}}}
        )
    )
    db = library["db"]
    edition = library["mayor"]
    shelf = make_user_book(db, user, edition.book,
                           read_state=ReadState.READING, hardcover_user_book_id=42)
    session_id = play(client, token, f"li_{edition.id}").json()["id"]

    response = post(client, token, f"/api/session/{session_id}/sync",
                    json={"currentTime": 95.0, "timeListened": 15, "duration": 100.0})
    assert response.status_code == 200

    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=edition.id).one()
    assert progress.is_finished is True
    assert progress.finished_at is not None
    db.refresh(shelf)
    assert shelf.read_state == ReadState.READ
    assert route.call_count == 1
    assert b"update_user_book" in route.calls[0].request.content


def test_session_close_final_sync_and_cleanup(client, token, library):
    db = library["db"]
    session_id = play(client, token, f"li_{library['mayor'].id}").json()["id"]

    response = post(client, token, f"/api/session/{session_id}/close",
                    json={"currentTime": 55.0, "duration": 100.0})
    assert response.status_code == 200
    assert session_id not in open_sessions

    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=library["mayor"].id).one()
    assert progress.current_time == 55.0


def test_sync_unknown_session_404(client, token, library):
    assert post(client, token, "/api/session/play_missing/sync",
                json={"currentTime": 1}).status_code == 404


def test_local_session_sync(client, token, library):
    db = library["db"]
    response = post(client, token, "/api/session/local",
                    json={"libraryItemId": f"li_{library['hail'].id}",
                          "currentTime": 12.0, "duration": 60.0, "timeListened": 12})
    assert response.status_code == 200

    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=library["hail"].id).one()
    assert progress.current_time == 12.0


def test_local_all_sessions(client, token, library):
    response = post(client, token, "/api/session/local-all",
                    json={"sessions": [
                        {"id": "s1", "libraryItemId": f"li_{library['hail'].id}",
                         "currentTime": 5.0, "duration": 60.0},
                        {"id": "s2", "libraryItemId": "li_99999", "currentTime": 5.0},
                    ]})
    body = response.json()
    assert body["results"][0]["success"] is True
    assert body["results"][1]["success"] is False


def test_patch_progress_finished_and_unfinished(client, token, library, tokenless_user):
    db = library["db"]
    item_id = f"li_{library['hail'].id}"

    response = client.patch(f"/api/me/progress/{item_id}", json={"isFinished": True},
                            headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=library["hail"].id).one()
    assert progress.is_finished is True
    shelf = db.query(UserBook).filter_by(
        user_id=tokenless_user.id, book_id=library["hail"].book.id).one()
    assert shelf.read_state == ReadState.READ

    response = client.patch(f"/api/me/progress/{item_id}", json={"isFinished": False},
                            headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    db.expire_all()
    progress = db.query(MediaProgress).filter_by(edition_id=library["hail"].id).one()
    assert progress.is_finished is False
    assert progress.finished_at is None


def test_get_progress(client, token, library, user):
    db = library["db"]
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=25.0, duration=100.0))
    db.commit()

    body = get(client, token, f"/api/me/progress/li_{library['mayor'].id}").json()
    assert body["currentTime"] == 25.0
    assert body["progress"] == pytest.approx(0.25)


def test_progress_isolated_between_users(client, token, library, user, db_session):
    """User B never sees user A's listening progress or gets their resume
    point; the shared catalogue stays visible to both."""
    from app.models import User
    from tests.conftest import cheap_password_hash

    other = db_session.query(User).filter_by(username="pat").one_or_none()
    if other is None:
        other = User(username="pat", password_hash=cheap_password_hash())
        db_session.add(other)
        db_session.commit()

    db = library["db"]
    item_id = f"li_{library['mayor'].id}"
    db.add(MediaProgress(user_id=user.id, edition_id=library["mayor"].id,
                         current_time=42.0, duration=100.0))
    db.commit()

    other_login = client.post(
        "/login", json={"username": "pat", "password": "hunter2"},
        headers={"x-return-tokens": "true"},
    ).json()
    other_token = other_login["user"]["accessToken"]

    # login payload progress lists are disjoint
    assert other_login["user"]["mediaProgress"] == []

    # user A resumes at 42s; user B starts from zero on the same shared book
    assert play(client, token, item_id).json()["startTime"] == 42.0
    assert play(client, other_token, item_id).json()["startTime"] == 0.0

    # explicit progress fetch 404s for the user without any
    assert get(client, other_token, f"/api/me/progress/{item_id}").status_code == 404
    assert get(client, token, f"/api/me/progress/{item_id}").status_code == 200

    # a session opened by A cannot be synced by B
    session_id = play(client, token, item_id).json()["id"]
    response = post(client, other_token, f"/api/session/{session_id}/sync",
                    json={"currentTime": 50.0, "duration": 100.0})
    assert response.status_code == 404
