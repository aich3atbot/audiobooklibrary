from datetime import date

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
    ReadState,
    Release,
    Series,
    UserBook,
)
from tests.conftest import make_user_book


@pytest.fixture(autouse=True)
def clean_db(db_session):
    for model in (UserBook, AudioFile, MediaProgress, Bookmark, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_library_page_empty(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No books yet" in response.text


def test_library_page_lists_books(client, user, db_session):
    author = Author(hardcover_id=1, name="Brandon Sanderson")
    series = Series(hardcover_id=1, name="The Stormlight Archive")
    book = Book(
        hardcover_id=100,
        title="The Way of Kings",
        author=author,
        series=series,
        series_index=1,
    )
    db_session.add(book)
    db_session.commit()
    make_user_book(db_session, user, book,
                   read_state=ReadState.READ, read_at=date(2024, 3, 1))

    response = client.get("/")
    assert response.status_code == 200
    assert "The Way of Kings" in response.text
    assert "Brandon Sanderson" in response.text
    assert "The Stormlight Archive" in response.text
    # the card links to the book detail page
    assert f'href="/books/{book.id}"' in response.text
