import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient
from app.models import (
    AppState,
    Author,
    Book,
    DownloadState,
    Edition,
    ReadState,
    Release,
    Series,
    UserBook,
)
from tests.conftest import make_user_book
from tests.test_sync import entry, me_response

ABB = "http://abb.test"


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


def series_book(book_id, title, position, users=0, author="Test Author"):
    return {
        "position": position,
        "book": {
            "id": book_id,
            "title": title,
            "release_year": 2020,
            "cached_image": {"url": f"https://assets.hardcover.app/{book_id}.jpg"},
            "users_count": users,
            "contributions": [{"author": {"id": 500, "name": author}}],
        },
    }


def series_response(name, rows):
    return httpx.Response(
        200, json={"data": {"series": [{"name": name}], "book_series": rows}}
    )


@respx.mock
def test_fetch_series_dedupes_and_orders():
    respx.post(API_URL).mock(
        return_value=series_response(
            "The Stormlight Archive",
            [
                series_book(1000, "The Way of Kings", 1.0, users=999),
                series_book(1001, "TWoK (other edition)", 1.0, users=3),
                series_book(2000, "Words of Radiance", 2.0, users=800),
                series_book(3000, "Companion Stories", None, users=10),
            ],
        )
    )

    with HardcoverClient("token") as client:
        name, books = client.fetch_series(300)

    assert name == "The Stormlight Archive"
    # one book per position (most-shelved edition wins), null positions kept
    assert [b["hardcover_id"] for b in books] == [1000, 2000, 3000]
    first = books[0]
    assert first["series_id"] == 300
    assert first["series_name"] == "The Stormlight Archive"
    assert first["series_position"] == 1.0
    assert first["authors"] == ["Test Author"]
    assert first["cover_url"] == "https://assets.hardcover.app/1000.jpg"


@respx.mock
def test_series_page_merges_local_state(client, clean_db, user):
    author = Author(hardcover_id=500, name="Test Author")
    shelved = Book(hardcover_id=1000, title="The Way of Kings", author=author)
    available = Book(hardcover_id=2000, title="Words of Radiance", author=author)
    available.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                      library_path="/audiobooks/x"))
    clean_db.add_all([shelved, available])
    clean_db.commit()
    make_user_book(clean_db, user, shelved, read_state=ReadState.READ)
    respx.post(API_URL).mock(
        return_value=series_response(
            "The Stormlight Archive",
            [
                series_book(1000, "The Way of Kings", 1.0),
                series_book(2000, "Words of Radiance", 2.0),
                series_book(3000, "Oathbringer", 3.0),
            ],
        )
    )

    response = client.get("/series/300")

    assert response.status_code == 200
    assert "The Stormlight Archive" in response.text
    # shelved book renders as a live library card
    assert f'hx-post="/books/{shelved.id}/read-state"' in response.text
    # in the shared store but unshelved: add to my library, never a download
    assert "Add to my library" in response.text
    assert '/series/2000/download' not in response.text
    # unknown book: addable and downloadable
    assert response.text.count('hx-post="/search/add"') == 2
    assert 'hx-post="/series/3000/download"' in response.text


@respx.mock
def test_series_page_hardcover_down(client, clean_db, user):
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("boom"))

    response = client.get("/series/300")

    assert response.status_code == 200
    assert "Hardcover series lookup failed" in response.text


def test_series_page_without_token(client, tokenless_user):
    response = client.get("/series/300")

    assert response.status_code == 200
    assert "No Hardcover token" in response.text


@respx.mock
def test_add_all_shelves_unshelved_books_only(client, clean_db, user):
    """Add-all shelves the known-but-unshelved and unknown books as
    want-to-read; the user's existing shelf entry is untouched."""
    author = Author(hardcover_id=500, name="Test Author")
    shelved = Book(hardcover_id=1000, title="The Way of Kings", author=author)
    available = Book(hardcover_id=2000, title="Words of Radiance", author=author)
    available.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                      library_path="/audiobooks/x"))
    clean_db.add_all([shelved, available])
    clean_db.commit()
    make_user_book(clean_db, user, shelved, read_state=ReadState.READ)
    rows = [
        series_book(1000, "The Way of Kings", 1.0),
        series_book(2000, "Words of Radiance", 2.0),
        series_book(3000, "Oathbringer", 3.0),
    ]
    hardcover = respx.post(API_URL)
    hardcover.side_effect = [
        series_response("The Stormlight Archive", rows),
        # 2000 is already a local Book: shelving pushes insert_user_book only
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 91, "error": None}}}),
        # 3000 is new: insert, then pull its metadata
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 92, "error": None}}}),
        me_response([entry(ub_id=92, status_id=1, book_id=3000, title="Oathbringer")]),
    ]

    response = client.post("/series/300/add-all")

    assert response.status_code == 200
    states = {
        ub.book.hardcover_id: ub.read_state
        for ub in clean_db.query(UserBook).filter(UserBook.user_id == user.id)
    }
    assert states == {
        1000: ReadState.READ,  # unchanged
        2000: ReadState.WANT_TO_READ,
        3000: ReadState.WANT_TO_READ,
    }
    # everything is shelved now: all cards, no add forms, no add-all button
    assert response.text.count('hx-post="/books/') == 3
    assert 'hx-post="/search/add"' not in response.text
    assert "Add all" not in response.text


@respx.mock
def test_add_all_survives_per_book_failures(client, clean_db, user):
    hardcover = respx.post(API_URL)
    hardcover.side_effect = [
        series_response("S", [series_book(1000, "A", 1.0), series_book(2000, "B", 2.0)]),
        httpx.ConnectError("boom"),  # first insert fails
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 91, "error": None}}}),
        me_response([entry(ub_id=91, status_id=1, book_id=2000, title="B")]),
    ]

    response = client.post("/series/300/add-all")

    assert response.status_code == 200
    assert "Added 1 of 2 books" in response.text
    states = [
        ub.book.hardcover_id
        for ub in clean_db.query(UserBook).filter(UserBook.user_id == user.id)
    ]
    assert states == [2000]
    assert "Add all (1)" in response.text  # the failed one is still addable


def test_add_all_without_token(client, clean_db, tokenless_user):
    response = client.post("/series/300/add-all")

    assert response.status_code == 200
    assert "No Hardcover token" in response.text


@pytest.fixture
def abb_config(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "index_url", ABB)


def mock_abb_search():
    from tests.test_downloads import fixture

    respx.get(f"{ABB}/").mock(return_value=httpx.Response(200, text=fixture("search.html")))
    respx.get(f"{ABB}/page/2/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )


@respx.mock
def test_series_download_adds_then_lists_releases(client, clean_db, user, abb_config):
    """Downloading an unshelved series book shelves it as want-to-read on
    Hardcover first, then opens the normal release picker."""
    hardcover = respx.post(API_URL)
    hardcover.side_effect = [
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 91, "error": None}}}),
        me_response([entry(ub_id=91, status_id=1, book_id=646489,
                           title="The Mayor of Noobtown", author_name="Ryan Rimmel")]),
    ]
    mock_abb_search()

    response = client.post("/series/646489/download")

    assert response.status_code == 200
    assert "Releases — The Mayor of Noobtown" in response.text
    ub = (
        clean_db.query(UserBook)
        .join(UserBook.book)
        .filter(UserBook.user_id == user.id, Book.hardcover_id == 646489)
        .one()
    )
    assert ub.read_state == ReadState.WANT_TO_READ
    assert hardcover.call_count == 2


@respx.mock
def test_series_download_shelved_skips_add(client, clean_db, user, abb_config):
    author = Author(hardcover_id=500, name="Ryan Rimmel")
    book = Book(hardcover_id=646489, title="The Mayor of Noobtown", author=author)
    clean_db.add(book)
    clean_db.commit()
    make_user_book(clean_db, user, book, read_state=ReadState.WANT_TO_READ)
    hardcover = respx.post(API_URL)
    mock_abb_search()

    response = client.post("/series/646489/download")

    assert response.status_code == 200
    assert "Releases — The Mayor of Noobtown" in response.text
    assert hardcover.call_count == 0


@respx.mock
def test_series_download_add_failure_shows_error(client, clean_db, user):
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("boom"))

    response = client.post("/series/646489/download")

    assert response.status_code == 200
    assert "Adding the book to your Hardcover shelf failed" in response.text
