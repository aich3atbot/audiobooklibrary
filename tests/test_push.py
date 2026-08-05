import json
from datetime import date

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient
from app.models import AppState, Author, Book, Edition, ReadState, Release, Series, UserBook
from app.services.sync import push_book, push_pending, sync_from_hardcover, update_read_state
from tests.conftest import make_user_book
from tests.test_sync import entry, me_response


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def book(clean_db):
    author = Author(hardcover_id=500, name="Test Author")
    book = Book(hardcover_id=1000, title="Test Book", author=author)
    clean_db.add(book)
    clean_db.commit()
    return book


@pytest.fixture
def shelf(clean_db, user, book):
    """The default user's shelf entry for the test book."""
    return make_user_book(
        clean_db, user, book,
        read_state=ReadState.WANT_TO_READ, hardcover_user_book_id=42,
    )


def mutation_response(field, id_=42):
    return httpx.Response(200, json={"data": {field: {"id": id_, "error": None}}})


def sent_graphql(route, call=0):
    return json.loads(route.calls[call].request.content)


@respx.mock
def test_push_updates_existing_user_book(clean_db, shelf):
    shelf.read_state = ReadState.READ
    shelf.read_at = date(2024, 5, 5)
    shelf.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, shelf)

    payload = sent_graphql(route)
    assert payload["variables"] == {
        "id": 42,
        "object": {"status_id": 3, "last_read_date": "2024-05-05"},
    }
    assert shelf.pending_push is False


@respx.mock
def test_push_sends_the_start_date(clean_db, shelf):
    shelf.read_state = ReadState.READING
    shelf.started_at = date(2024, 5, 1)
    shelf.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, shelf)

    assert sent_graphql(route)["variables"] == {
        "id": 42,
        "object": {"status_id": 2, "first_started_reading_date": "2024-05-01"},
    }


@respx.mock
def test_push_inserts_when_not_on_shelf(clean_db, shelf):
    shelf.hardcover_user_book_id = None
    shelf.read_state = ReadState.READING
    shelf.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("insert_user_book", id_=77))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, shelf)

    payload = sent_graphql(route)
    assert payload["variables"] == {"object": {"book_id": 1000, "status_id": 2}}
    assert shelf.hardcover_user_book_id == 77


@respx.mock
def test_push_none_deletes_user_book(clean_db, shelf):
    shelf.read_state = ReadState.NONE
    shelf.pending_push = True
    route = respx.post(API_URL).mock(return_value=mutation_response("delete_user_book"))

    with HardcoverClient("token") as client:
        push_book(clean_db, client, shelf)

    assert sent_graphql(route)["variables"] == {"id": 42}
    assert shelf.hardcover_user_book_id is None
    assert shelf.pending_push is False


@respx.mock
def test_push_failure_keeps_pending(clean_db, user, shelf):
    shelf.read_state = ReadState.READ
    shelf.pending_push = True
    clean_db.commit()
    respx.post(API_URL).mock(return_value=httpx.Response(500))

    with HardcoverClient("token") as client:
        pushed = push_pending(clean_db, client, user)

    assert pushed == 0
    clean_db.refresh(shelf)
    assert shelf.pending_push is True


@respx.mock
def test_pull_skips_pending_books(clean_db, user, shelf):
    shelf.read_state = ReadState.READ
    shelf.read_at = date(2024, 5, 5)
    shelf.pending_push = True
    clean_db.commit()
    # Hardcover still reports the stale want-to-read state
    respx.post(API_URL).mock(
        return_value=me_response([entry(ub_id=42, status_id=1, book_id=1000)])
    )

    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client, user)

    clean_db.refresh(shelf)
    assert shelf.read_state == ReadState.READ
    assert shelf.read_at == date(2024, 5, 5)
    assert shelf.pending_push is True


@respx.mock
def test_update_read_state_marks_read_with_today(clean_db, user, book, shelf):
    respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    ub = update_read_state(clean_db, user, book, ReadState.READ)

    assert ub.read_state == ReadState.READ
    assert ub.read_at == date.today()
    assert ub.pending_push is False


@respx.mock
def test_update_read_state_creates_shelf_entry_for_available_book(clean_db, user, book):
    """A book in the shared store but not in the user's library gets a shelf
    entry (and a Hardcover insert) when they set a read state."""
    route = respx.post(API_URL).mock(return_value=mutation_response("insert_user_book", id_=77))

    ub = update_read_state(clean_db, user, book, ReadState.WANT_TO_READ)

    assert ub.user_id == user.id
    assert ub.book_id == book.id
    assert ub.hardcover_user_book_id == 77
    assert b"insert_user_book" in route.calls[0].request.content


@respx.mock
def test_update_read_state_stays_pending_when_hardcover_down(clean_db, user, book, shelf):
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("down"))

    update_read_state(clean_db, user, book, ReadState.READING)

    clean_db.refresh(shelf)
    assert shelf.read_state == ReadState.READING
    assert shelf.pending_push is True


def test_update_read_state_without_token_stays_pending(clean_db, tokenless_user, book):
    ub = update_read_state(clean_db, tokenless_user, book, ReadState.READING)
    assert ub.read_state == ReadState.READING
    assert ub.pending_push is True


@respx.mock
def test_read_state_route_returns_updated_card(client, clean_db, book, shelf):
    respx.post(API_URL).mock(return_value=mutation_response("update_user_book"))

    response = client.post(f"/books/{book.id}/read-state", data={"state": "read"})

    assert response.status_code == 200
    assert "selected" in response.text
    assert f'id="book-{book.id}"' in response.text


def test_read_state_route_rejects_bad_state(client, clean_db, book):
    response = client.post(f"/books/{book.id}/read-state", data={"state": "bogus"})
    assert response.status_code == 422
