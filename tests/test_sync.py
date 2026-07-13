from datetime import date

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, PAGE_SIZE, HardcoverClient
from app.models import AppState, Author, Book, ReadState, Release, Series
from app.services.sync import parse_read_at, pick_series, sync_from_hardcover


@pytest.fixture
def clean_db(db_session):
    for model in (Release, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


def me_response(user_books):
    return httpx.Response(200, json={"data": {"me": [{"user_books": user_books}]}})


def entry(
    ub_id=1,
    status_id=1,
    book_id=1000,
    title="Test Book",
    author_id=500,
    author_name="Test Author",
    book_series=None,
    last_read_date=None,
    reads=None,
    cover=None,
):
    return {
        "id": ub_id,
        "status_id": status_id,
        "last_read_date": last_read_date,
        "book": {
            "id": book_id,
            "title": title,
            "cached_image": {"url": cover} if cover else None,
            "contributions": [{"author": {"id": author_id, "name": author_name}}],
            "book_series": book_series or [],
        },
        "user_book_reads": reads or [],
    }


def test_pick_series_prefers_featured_then_lowest_id():
    chosen, position = pick_series(
        [
            {"position": 5, "featured": False, "series": {"id": 1, "name": "Not featured"}},
            {"position": 3, "featured": True, "series": {"id": 20, "name": "Featured B"}},
            {"position": 2, "featured": True, "series": {"id": 10, "name": "Featured A"}},
        ]
    )
    assert chosen["name"] == "Featured A"
    assert position == 2


def test_pick_series_empty():
    assert pick_series([]) == (None, None)


def test_parse_read_at_takes_latest_date():
    result = parse_read_at(
        {
            "last_read_date": "2024-01-01",
            "user_book_reads": [{"finished_at": "2024-06-15"}, {"finished_at": None}],
        }
    )
    assert result == date(2024, 6, 15)


def test_parse_read_at_none_when_no_dates():
    assert parse_read_at({"last_read_date": None, "user_book_reads": []}) is None


@respx.mock
def test_sync_creates_books(clean_db):
    respx.post(API_URL).mock(
        return_value=me_response(
            [
                entry(
                    status_id=3,
                    book_id=1000,
                    title="The Way of Kings",
                    author_id=500,
                    author_name="Brandon Sanderson",
                    book_series=[
                        {
                            "position": 1,
                            "featured": True,
                            "series": {"id": 300, "name": "The Stormlight Archive"},
                        }
                    ],
                    last_read_date="2024-03-01",
                    cover="https://assets.hardcover.app/cover.jpg",
                ),
                entry(status_id=2, ub_id=2, book_id=1001, title="Standalone", author_id=500,
                      author_name="Brandon Sanderson"),
            ]
        )
    )

    with HardcoverClient("token") as client:
        result = sync_from_hardcover(clean_db, client)

    assert result == {"created": 2, "updated": 0, "total": 2}
    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    assert book.title == "The Way of Kings"
    assert book.author.name == "Brandon Sanderson"
    assert book.series.name == "The Stormlight Archive"
    assert book.series_index == 1
    assert book.read_state == ReadState.READ
    assert book.read_at == date(2024, 3, 1)
    assert book.cover_url == "https://assets.hardcover.app/cover.jpg"
    # one shared author row, standalone book has no series
    assert clean_db.query(Author).count() == 1
    other = clean_db.query(Book).filter_by(hardcover_id=1001).one()
    assert other.series is None
    assert other.read_state == ReadState.READING


@respx.mock
def test_resync_updates_in_place(clean_db):
    route = respx.post(API_URL)
    route.mock(return_value=me_response([entry(status_id=1, book_id=1000)]))
    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client)

    route.mock(
        return_value=me_response([entry(status_id=3, book_id=1000, last_read_date="2024-05-05")])
    )
    with HardcoverClient("token") as client:
        result = sync_from_hardcover(clean_db, client)

    assert result == {"created": 0, "updated": 1, "total": 1}
    assert clean_db.query(Book).count() == 1
    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    assert book.read_state == ReadState.READ
    assert book.read_at == date(2024, 5, 5)


@respx.mock
def test_sync_does_not_touch_download_state(clean_db):
    respx.post(API_URL).mock(return_value=me_response([entry(book_id=1000)]))
    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client)

    from app.models import DownloadState

    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    book.download_state = DownloadState.IMPORTED
    book.library_path = "/audiobooks/x"
    clean_db.commit()

    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client)
    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    assert book.download_state == DownloadState.IMPORTED
    assert book.library_path == "/audiobooks/x"


@respx.mock
def test_pagination_follows_full_pages(clean_db):
    full_page = [entry(ub_id=i, book_id=2000 + i) for i in range(PAGE_SIZE)]
    partial_page = [entry(ub_id=999, book_id=9999)]
    route = respx.post(API_URL)
    route.side_effect = [me_response(full_page), me_response(partial_page)]

    with HardcoverClient("token") as client:
        books = client.fetch_user_books()

    assert len(books) == PAGE_SIZE + 1
    assert route.call_count == 2


@respx.mock
def test_bearer_prefix_stripped_from_token():
    route = respx.post(API_URL).mock(return_value=me_response([]))
    with HardcoverClient("Bearer abc123") as client:
        client.fetch_user_books()
    assert route.calls[0].request.headers["Authorization"] == "Bearer abc123"
