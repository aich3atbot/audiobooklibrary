from datetime import date

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, PAGE_SIZE, HardcoverClient
from app.models import AppState, Author, Book, ReadState, Release, Series, UserBook
from app.services.sync import (
    backfill_author_images,
    backfill_hardcover_slugs,
    parse_read_at,
    parse_started_at,
    pick_series,
    sync_from_hardcover,
)


@pytest.fixture
def clean_db(db_session):
    from app.models import Edition

    for model in (UserBook, Release, Edition, Book, Author, Series, AppState):
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
    first_started_reading_date=None,
    reads=None,
    cover=None,
    author_image=None,
    slug=None,
):
    return {
        "id": ub_id,
        "status_id": status_id,
        "last_read_date": last_read_date,
        "first_started_reading_date": first_started_reading_date,
        "book": {
            "id": book_id,
            "title": title,
            "slug": slug,
            "cached_image": {"url": cover} if cover else None,
            "contributions": [
                {
                    "author": {
                        "id": author_id,
                        "name": author_name,
                        "cached_image": {"url": author_image} if author_image else None,
                    }
                }
            ],
            "book_series": book_series or [],
        },
        "user_book_reads": reads or [],
    }


def user_book_for(db, user, hardcover_id) -> UserBook:
    return (
        db.query(UserBook)
        .join(UserBook.book)
        .filter(UserBook.user_id == user.id, Book.hardcover_id == hardcover_id)
        .one()
    )


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


def test_parse_started_at_takes_earliest_date():
    result = parse_started_at(
        {
            "first_started_reading_date": "2024-02-01",
            "user_book_reads": [{"started_at": "2024-01-05"}, {"started_at": None}],
        }
    )
    assert result == date(2024, 1, 5)


def test_parse_started_at_none_when_no_dates():
    assert parse_started_at({"first_started_reading_date": None, "user_book_reads": []}) is None


@respx.mock
def test_sync_creates_books(clean_db, user):
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
                    first_started_reading_date="2024-02-10",
                    cover="https://assets.hardcover.app/cover.jpg",
                ),
                entry(status_id=2, ub_id=2, book_id=1001, title="Standalone", author_id=500,
                      author_name="Brandon Sanderson",
                      author_image="https://assets.hardcover.app/authors/500/bs.jpg"),
            ]
        )
    )

    with HardcoverClient("token") as client:
        result = sync_from_hardcover(clean_db, client, user)

    assert result == {"created": 2, "updated": 0, "total": 2}
    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    assert book.title == "The Way of Kings"
    assert book.author.name == "Brandon Sanderson"
    assert book.series.name == "The Stormlight Archive"
    assert book.series_index == 1
    assert book.cover_url == "https://assets.hardcover.app/cover.jpg"
    ub = user_book_for(clean_db, user, 1000)
    assert ub.read_state == ReadState.READ
    assert ub.read_at == date(2024, 3, 1)
    assert ub.started_at == date(2024, 2, 10)
    # one shared author row, standalone book has no series
    assert clean_db.query(Author).count() == 1
    assert book.author.image_url == "https://assets.hardcover.app/authors/500/bs.jpg"
    other = clean_db.query(Book).filter_by(hardcover_id=1001).one()
    assert other.series is None
    assert user_book_for(clean_db, user, 1001).read_state == ReadState.READING
    # sync cursor recorded on the user
    assert user.last_sync_at is not None
    assert user.last_sync_result.startswith("ok:")


@respx.mock
def test_resync_updates_in_place(clean_db, user):
    route = respx.post(API_URL)
    route.mock(return_value=me_response([entry(status_id=1, book_id=1000)]))
    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client, user)

    route.mock(
        return_value=me_response([entry(status_id=3, book_id=1000, last_read_date="2024-05-05")])
    )
    with HardcoverClient("token") as client:
        result = sync_from_hardcover(clean_db, client, user)

    assert result == {"created": 0, "updated": 1, "total": 1}
    assert clean_db.query(Book).count() == 1
    assert clean_db.query(UserBook).count() == 1
    ub = user_book_for(clean_db, user, 1000)
    assert ub.read_state == ReadState.READ
    assert ub.read_at == date(2024, 5, 5)


@respx.mock
def test_two_users_sync_disjoint_libraries_over_shared_books(clean_db, user, db_session):
    """Each user's sync builds their own shelf; a book both users have exists
    once with two memberships."""
    from app.models import User
    from tests.conftest import cheap_password_hash

    other = db_session.query(User).filter_by(username="mallory").one_or_none()
    if other is None:
        other = User(username="mallory", password_hash=cheap_password_hash(), hardcover_token="t2")
        db_session.add(other)
        db_session.commit()

    route = respx.post(API_URL)
    route.mock(return_value=me_response([entry(ub_id=1, status_id=1, book_id=1000),
                                         entry(ub_id=2, status_id=1, book_id=1001)]))
    with HardcoverClient("t1") as client:
        sync_from_hardcover(clean_db, client, user)

    route.mock(return_value=me_response([entry(ub_id=9, status_id=3, book_id=1001,
                                               last_read_date="2024-01-01")]))
    with HardcoverClient("t2") as client:
        sync_from_hardcover(clean_db, client, other)

    assert clean_db.query(Book).count() == 2  # 1001 is shared, not duplicated
    assert clean_db.query(UserBook).filter_by(user_id=user.id).count() == 2
    assert clean_db.query(UserBook).filter_by(user_id=other.id).count() == 1
    # read state is per user
    assert user_book_for(clean_db, user, 1001).read_state == ReadState.WANT_TO_READ
    mine = (
        clean_db.query(UserBook)
        .join(UserBook.book)
        .filter(UserBook.user_id == other.id, Book.hardcover_id == 1001)
        .one()
    )
    assert mine.read_state == ReadState.READ


@respx.mock
def test_sync_does_not_touch_download_state(clean_db, user):
    respx.post(API_URL).mock(return_value=me_response([entry(book_id=1000)]))
    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client, user)

    from app.models import DownloadState, Edition

    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    edition = Edition(
        book=book, download_state=DownloadState.IMPORTED, library_path="/audiobooks/x"
    )
    clean_db.add(edition)
    clean_db.commit()

    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client, user)
    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    assert book.editions[0].download_state == DownloadState.IMPORTED
    assert book.editions[0].library_path == "/audiobooks/x"


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


@respx.mock
def test_backfill_author_images_fills_and_marks_the_rest(clean_db):
    """Authors stored before we asked Hardcover for photos get filled in once;
    an author Hardcover has no photo for is marked so we stop asking."""
    with_photo = Author(hardcover_id=500, name="Brandon Sanderson")
    without = Author(hardcover_id=501, name="Obscure Author")
    local_only = Author(hardcover_id=None, name="Hand Added")
    clean_db.add_all([with_photo, without, local_only])
    clean_db.commit()

    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json={"data": {"authors": [
            {"id": 500, "cached_image": {"url": "https://assets.hardcover.app/a/500.jpg"}},
            {"id": 501, "cached_image": None},
        ]}})
    )

    with HardcoverClient("token") as client:
        assert backfill_author_images(clean_db, client) == 1

        clean_db.expire_all()
        assert with_photo.image_url == "https://assets.hardcover.app/a/500.jpg"
        assert without.image_url == ""
        assert local_only.image_url is None  # no hardcover id, nothing to ask

        # nothing left to look up: no second request
        assert backfill_author_images(clean_db, client) == 0
    assert route.call_count == 1


@respx.mock
def test_sync_stores_hardcover_slugs(clean_db, user):
    """hardcover.app routes on slugs, so sync records the book's and its
    series' — both ride along on the query we already make."""
    respx.post(API_URL).mock(
        return_value=me_response([
            entry(
                book_id=1000,
                title="The Way of Kings",
                slug="the-way-of-kings",
                book_series=[{
                    "position": 1,
                    "featured": True,
                    "series": {"id": 300, "name": "The Stormlight Archive",
                               "slug": "the-stormlight-archive"},
                }],
            )
        ])
    )

    with HardcoverClient("token") as client:
        sync_from_hardcover(clean_db, client, user)

    book = clean_db.query(Book).filter_by(hardcover_id=1000).one()
    assert book.hardcover_slug == "the-way-of-kings"
    assert book.series.hardcover_slug == "the-stormlight-archive"


@respx.mock
def test_backfill_hardcover_slugs_fills_books_and_series(clean_db):
    """Rows stored before we asked Hardcover for slugs get filled in once; a
    row Hardcover has no slug for is marked so we stop asking."""
    author = Author(hardcover_id=500, name="Brandon Sanderson")
    series = Series(hardcover_id=300, name="The Stormlight Archive")
    known = Book(hardcover_id=1000, title="The Way of Kings", author=author, series=series)
    unknown = Book(hardcover_id=1001, title="Obscure Book", author=author)
    clean_db.add_all([known, unknown])
    clean_db.commit()

    respx.post(API_URL).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"books": [
                {"id": 1000, "slug": "the-way-of-kings"},
                {"id": 1001, "slug": None},
            ]}}),
            httpx.Response(200, json={"data": {"series": [
                {"id": 300, "slug": "the-stormlight-archive"},
            ]}}),
        ]
    )

    with HardcoverClient("token") as client:
        assert backfill_hardcover_slugs(clean_db, client) == 2

        clean_db.expire_all()
        assert known.hardcover_slug == "the-way-of-kings"
        assert unknown.hardcover_slug == ""
        assert series.hardcover_slug == "the-stormlight-archive"

        # nothing left to look up: no further requests
        assert backfill_hardcover_slugs(clean_db, client) == 0


@respx.mock
def test_sync_skips_a_limited_user(clean_db, limited_user):
    """A limited account has no Hardcover identity, so the sync pass must not
    reach for one — not even if a token somehow ends up on the row. And its
    skip is not an error to report: last_sync_result stays untouched."""
    from app.services.sync import run_sync_all, run_sync_for_user

    limited_user.hardcover_token = "limited-token"
    limited_user.last_sync_result = None
    clean_db.commit()

    route = respx.post(API_URL).mock(return_value=me_response([]))

    assert run_sync_for_user(limited_user.id) == {"created": 0, "updated": 0, "total": 0}
    run_sync_all()

    # other accounts in the shared test database may legitimately sync; none
    # of the calls may carry this user's token
    tokens_used = [c.request.headers.get("authorization") for c in route.calls]
    assert "Bearer limited-token" not in tokens_used

    clean_db.expire_all()
    assert limited_user.last_sync_result is None
