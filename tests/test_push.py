import json
from datetime import date

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient
from app.models import AppState, Author, Book, ReadState, Release, Series
from app.services.sync import push_book, push_pending, sync_from_hardcover, update_read_state
from tests.test_sync import entry, me_response


@pytest.fixture
def clean_db(db_session):
    for model in (Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def book(clean_db):
    author = Author(hardcover_id=500, name="Test Author")
    book = Book(
        hardcover_id=1000,
        title="Test Book",
        author=author,
        read_state=ReadState.WANT_TO_READ,
        hardcover_user_book_id=42,
    )
    clean_db.add(book)
    clean_db.commit()
    return book


def mutation_response(field, id_=42):
    return httpx.Response(200, json={"data": {field: {"id": id_, "error": None}}})


def sent_graphql(route, call=0):
    return json.loads(route.calls[call].request.content)


@respx.mock
def test_push_updates_existing_user_book(clean_db, book):
    book.read_state = ReadState.READ
    book.read_at = date(2024, 5, 5)
    book.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, book)

    payload = sent_graphql(route)
    assert payload["variables"] == {
        "id": 42,
        "object": {"status_id": 3, "last_read_date": "2024-05-05"},
    }
    assert book.pending_push is False


@respx.mock
def test_push_inserts_when_not_on_shelf(clean_db, book):
    book.hardcover_user_book_id = None
    book.read_state = ReadState.READING
    book.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("insert_user_book", id_=77))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, book)

    payload = sent_graphql(route)
    assert payload["variables"] == {"object": {"book_id": 1000, "status_id": 2}}
    assert book.hardcover_user_book_id == 77


@respx.mock
def test_push_none_deletes_user_book(clean_db, book):
    book.read_state = ReadState.NONE
    book.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("delete_user_book"))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, book)

    assert sent_graphql(route)["variables"] == {"id": 42}
    assert book.hardcover_user_book_id is None
    assert book.pending_push is False


@respx.mock
def test_push_failure_keeps_pending(clean_db, book):
    book.read_state = ReadState.READ
    book.pending_push = True
    clean_db.commit()
    respx.post(API_URL).mock(return_value=httpx.Response(500))

    with HardcoverClient("token") as client:
        pushed = push_pending(clean_db, client)

    assert pushed == 0
    clean_db.refresh(book)
    assert book.pending_push is True


@respx.mock
def test_pull_skips_pending_books(clean_db, book):
    book.read_state = ReadState.READ
    book.read_at = date(2024, 5, 5)
    book.pending_push = True
    clean_db.commit()
    # Hardcover still reports the stale want-to-read state
    respx.post(API_URL).mock(
        return_value=me_response([entry(ub_id=42, status_id=1, book_id=1000)])
    )

    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client)

    clean_db.refresh(book)
    assert book.read_state == ReadState.READ
    assert book.read_at == date(2024, 5, 5)
    assert book.pending_push is True


@respx.mock
def test_update_read_state_marks_read_with_today(clean_db, book, test_settings, monkeypatch):
    monkeypatch.setattr(test_settings.__class__, "hardcover_token", "token", raising=False)
    respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    update_read_state(clean_db, book, ReadState.READ)

    assert book.read_state == ReadState.READ
    assert book.read_at == date.today()
    assert book.pending_push is False


@respx.mock
def test_update_read_state_stays_pending_when_hardcover_down(
    clean_db, book, test_settings, monkeypatch
):
    monkeypatch.setattr(test_settings.__class__, "hardcover_token", "token", raising=False)
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("down"))

    update_read_state(clean_db, book, ReadState.READING)

    clean_db.refresh(book)
    assert book.read_state == ReadState.READING
    assert book.pending_push is True


@respx.mock
def test_read_state_route_returns_updated_card(client, clean_db, book, test_settings, monkeypatch):
    monkeypatch.setattr(test_settings.__class__, "hardcover_token", "token", raising=False)
    respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    response = client.post(f"/books/{book.id}/read-state", data={"state": "read"})

    assert response.status_code == 200
    assert "selected" in response.text
    assert f'id="book-{book.id}"' in response.text


def test_read_state_route_rejects_bad_state(client, clean_db, book):
    response = client.post(f"/books/{book.id}/read-state", data={"state": "bogus"})
    assert response.status_code == 422
