import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient, _parse_search_document
from app.models import AppState, Author, Book, ReadState, Release, Series
from app.services.sync import add_book
from tests.test_sync import entry, me_response


@pytest.fixture
def clean_db(db_session):
    for model in (Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def hardcover_token(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "hardcover_token", "token")


def search_doc(**overrides):
    doc = {
        "id": "1000",
        "title": "The Way of Kings",
        "author_names": ["Brandon Sanderson", "Michael Kramer"],
        "contribution_types": ["Author", "Narrator"],
        "contributions": [
            {"author": {"id": 500, "name": "Brandon Sanderson"}},
            {"author": {"id": 501, "name": "Michael Kramer"}},
        ],
        "featured_series": {
            "position": 1.0,
            "series": {"id": 300, "name": "The Stormlight Archive"},
        },
        "image": {"url": "https://assets.hardcover.app/cover.jpg"},
        "release_year": 2010,
        "has_audiobook": True,
        "users_count": 999,
    }
    doc.update(overrides)
    return doc


def search_response(docs):
    return httpx.Response(
        200,
        json={
            "data": {
                "search": {
                    "results": {"found": len(docs), "hits": [{"document": d} for d in docs]}
                }
            }
        },
    )


def test_parse_search_document_filters_narrators():
    parsed = _parse_search_document(search_doc())
    assert parsed["authors"] == ["Brandon Sanderson"]
    assert parsed["hardcover_id"] == 1000
    assert parsed["series_name"] == "The Stormlight Archive"
    assert parsed["series_position"] == 1.0
    assert parsed["has_audiobook"] is True


def test_parse_search_document_falls_back_to_author_names():
    parsed = _parse_search_document(
        search_doc(contributions=[], contribution_types=[], author_names=["Someone"])
    )
    assert parsed["authors"] == ["Someone"]


def test_parse_search_document_no_series():
    parsed = _parse_search_document(search_doc(featured_series=None))
    assert parsed["series_name"] is None
    assert parsed["series_position"] is None


@respx.mock
def test_search_books(hardcover_token):
    respx.post(API_URL).mock(return_value=search_response([search_doc()]))
    with HardcoverClient("token") as client:
        results = client.search_books("way of kings")
    assert len(results) == 1
    assert results[0]["title"] == "The Way of Kings"


@respx.mock
def test_add_book_inserts_then_pulls_metadata(clean_db, hardcover_token):
    route = respx.post(API_URL)
    route.side_effect = [
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 42, "error": None}}}),
        me_response([entry(ub_id=42, status_id=2, book_id=1000, title="Added Book")]),
    ]

    book = add_book(clean_db, 1000, ReadState.READING)

    assert book.hardcover_id == 1000
    assert book.title == "Added Book"
    assert book.hardcover_user_book_id == 42
    assert book.read_state == ReadState.READING
    assert book.pending_push is False
    assert route.call_count == 2


@respx.mock
def test_add_book_existing_updates_state_without_insert(clean_db, hardcover_token):
    author = Author(hardcover_id=500, name="A")
    existing = Book(
        hardcover_id=1000,
        title="Existing",
        author=author,
        read_state=ReadState.WANT_TO_READ,
        hardcover_user_book_id=42,
    )
    clean_db.add(existing)
    clean_db.commit()

    route = respx.post(API_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"update_user_book": {"id": 42, "error": None}}}
        )
    )

    book = add_book(clean_db, 1000, ReadState.READING)

    assert book.id == existing.id
    assert book.read_state == ReadState.READING
    assert route.call_count == 1
    assert b"update_user_book" in route.calls[0].request.content


@respx.mock
def test_search_page_marks_books_in_library(client, clean_db, hardcover_token):
    author = Author(hardcover_id=500, name="Brandon Sanderson")
    clean_db.add(
        Book(hardcover_id=1000, title="The Way of Kings", author=author,
             read_state=ReadState.READ)
    )
    clean_db.commit()
    respx.post(API_URL).mock(
        return_value=search_response([search_doc(), search_doc(id="2000", title="Other Book")])
    )

    response = client.get("/search", params={"q": "way of kings"})

    assert response.status_code == 200
    # in-library book renders as a library card (read-state select), the
    # other as a search result with an Add button
    assert response.text.count("hx-post=\"/search/add\"") == 1
    assert "/books/" in response.text


@respx.mock
def test_search_add_route_returns_library_card(client, clean_db, hardcover_token):
    respx.post(API_URL).side_effect = [
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 42, "error": None}}}),
        me_response([entry(ub_id=42, status_id=1, book_id=1000, title="Added Book")]),
    ]

    response = client.post(
        "/search/add", data={"hardcover_id": "1000", "state": "want_to_read"}
    )

    assert response.status_code == 200
    assert "read-state" in response.text
    assert "Added Book" in response.text


def test_search_add_route_rejects_none_state(client, clean_db):
    response = client.post("/search/add", data={"hardcover_id": "1000", "state": "none"})
    assert response.status_code == 422
