import json
import shutil

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient
from app.models import AppState, Author, Book, DownloadState, Edition, Release, Series, UserBook
from app.services.collection import (
    entry_for,
    hardcover_query,
    identify_entry,
    import_entry,
    scan_imports,
    score_result,
)
from app.services.importer import ImportFailure


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def dirs(test_settings):
    for d in (test_settings.imports_dir, test_settings.library_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    return test_settings


@pytest.fixture
def book(clean_db):
    author = Author(hardcover_id=500, name="Ryan Rimmel")
    series = Series(hardcover_id=300, name="Noobtown")
    book = Book(
        hardcover_id=646489,
        title="The Mayor of Noobtown",
        author=author,
        series=series,
        series_index=1.0,
    )
    clean_db.add(book)
    clean_db.commit()
    return book


@pytest.fixture(autouse=True)
def no_background_sync(monkeypatch):
    """Imports kick a background all-user sync; keep it out of tests."""
    kicks = []
    monkeypatch.setattr("app.routes.imports._schedule_sync_all", lambda: kicks.append(1))
    return kicks


def put(dirs, rel, files=("part1.mp3", "part2.mp3")):
    folder = dirs.imports_dir / rel
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / f).write_bytes(b"x" * 10)
    return folder


MAYOR_DOC = {
    "hardcover_id": 646489,
    "title": "The Mayor of Noobtown",
    "authors": ["Ryan Rimmel"],
    "series_name": "Noobtown",
    "series_position": 1.0,
    "cover_url": None,
    "release_year": 2019,
    "has_audiobook": True,
    "users_count": 10,
}


def typesense_doc(parsed):
    """Turn a parsed-result dict back into the raw search document shape."""
    return {
        "id": str(parsed["hardcover_id"]),
        "title": parsed["title"],
        "author_names": parsed["authors"],
        "contribution_types": ["Author"] * len(parsed["authors"]),
        "contributions": [
            {"author": {"id": 1, "name": name}} for name in parsed["authors"]
        ],
        "featured_series": (
            {"position": parsed["series_position"], "series": {"id": 2, "name": parsed["series_name"]}}
            if parsed.get("series_name")
            else None
        ),
        "image": {"url": parsed.get("cover_url")},
        "release_year": parsed.get("release_year"),
        "has_audiobook": parsed.get("has_audiobook", False),
        "users_count": parsed.get("users_count", 0),
    }


def search_response(parsed_docs):
    hits = [{"document": typesense_doc(d)} for d in parsed_docs]
    return httpx.Response(
        200,
        json={"data": {"search": {"results": {"found": len(hits), "hits": hits}}}},
    )


def book_response(hardcover_id, title, author=("Ryan Rimmel", 500), series=None):
    doc = {
        "id": hardcover_id,
        "title": title,
        "cached_image": None,
        "contributions": [{"author": {"id": author[1], "name": author[0]}}],
        "book_series": (
            [{"position": series[2], "featured": True,
              "series": {"id": series[1], "name": series[0]}}]
            if series
            else []
        ),
    }
    return httpx.Response(200, json={"data": {"books": [doc]}})


def hardcover_dispatch(search_docs, books=None):
    """Answer search queries with search_docs and books-by-id from books."""
    books = books or {}

    def handle(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "search(" in query:
            return search_response(search_docs)
        if "books(" in query:
            book_id = json.loads(request.content)["variables"]["id"]
            if book_id in books:
                return books[book_id]
            return httpx.Response(200, json={"data": {"books": []}})
        raise AssertionError(f"unexpected Hardcover query: {query[:80]}")

    return respx.post(API_URL).mock(side_effect=handle)


# --- scanner ---------------------------------------------------------------


def test_scan_nested_leaf_folders(dirs):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown")
    put(dirs, "Ryan Rimmel/Noobtown/02 - Village of Noobtown")

    entries = scan_imports()

    assert sorted(e.rel for e in entries) == [
        "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown",
        "Ryan Rimmel/Noobtown/02 - Village of Noobtown",
    ]
    assert entries[0].audio_files == 2
    assert entries[0].size == 20


def test_scan_groups_disc_folders(dirs):
    put(dirs, "Some Book/CD1")
    put(dirs, "Some Book/CD2")

    entries = scan_imports()

    assert [e.rel for e in entries] == ["Some Book"]
    assert entries[0].audio_files == 4


def test_scan_loose_files_and_hidden_skipped(dirs):
    (dirs.imports_dir / "Standalone Book.m4b").write_bytes(b"x" * 5)
    (dirs.imports_dir / ".hidden").mkdir()
    (dirs.imports_dir / ".hidden" / "x.mp3").write_bytes(b"x")
    (dirs.imports_dir / "notes.txt").write_bytes(b"x")

    entries = scan_imports()

    assert [e.rel for e in entries] == ["Standalone Book.m4b"]
    assert entries[0].name == "Standalone Book"


def test_scan_ignores_folders_without_audio(dirs):
    put(dirs, "Real Book")
    (dirs.imports_dir / "Empty/Deeper").mkdir(parents=True)

    entries = scan_imports()

    assert [e.rel for e in entries] == ["Real Book"]


def test_entry_for_rejects_traversal(dirs):
    put(dirs, "Real Book")
    assert entry_for("Real Book") is not None
    assert entry_for("../outside") is None
    assert entry_for(".") is None
    assert entry_for("Gone") is None


# --- identification --------------------------------------------------------


def test_hardcover_query_uses_parent_folders_without_duplicates(dirs):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown")
    entry = scan_imports()[0]
    assert hardcover_query(entry) == "ryan rimmel noobtown 01 the mayor of"


def test_score_result_exact_title(dirs):
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]
    assert score_result(entry, MAYOR_DOC) > 0.9


def test_score_result_with_noise_and_author_path(dirs):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown [M4B 64kbps]")
    entry = scan_imports()[0]
    assert score_result(entry, MAYOR_DOC) >= 0.75


def test_score_result_unrelated_is_low(dirs):
    put(dirs, "A Completely Different Story by Nobody")
    entry = scan_imports()[0]
    assert score_result(entry, MAYOR_DOC) < 0.5


@respx.mock
def test_identify_entry_picks_best_result(dirs):
    put(dirs, "The Mayor of Noobtown")
    other = dict(MAYOR_DOC, hardcover_id=111, title="Completely Unrelated Novel")
    hardcover_dispatch([other, MAYOR_DOC])
    entry = scan_imports()[0]

    with HardcoverClient("token") as client:
        match = identify_entry(client, entry)

    assert match["hardcover_id"] == 646489
    assert match["title"] == "The Mayor of Noobtown"
    assert match["author"] == "Ryan Rimmel"
    assert match["score"] > 0.9


@respx.mock
def test_identify_entry_below_threshold_is_none(dirs):
    put(dirs, "Zzz Qqq Xxx")
    hardcover_dispatch([MAYOR_DOC])
    entry = scan_imports()[0]

    with HardcoverClient("token") as client:
        assert identify_entry(client, entry) is None


# --- import service ---------------------------------------------------------


def test_import_entry_moves_folder_and_cleans_up(clean_db, dirs, book):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown")
    entry = scan_imports()[0]

    dest = import_entry(clean_db, book, entry)

    assert dest == dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert sorted(p.name for p in dest.iterdir()) == ["part1.mp3", "part2.mp3"]
    # source folder and now-empty parents are gone; imports root remains
    assert not (dirs.imports_dir / "Ryan Rimmel").exists()
    assert dirs.imports_dir.exists()
    edition = book.editions[0]
    assert edition.label == ""
    assert edition.download_state == DownloadState.IMPORTED
    assert edition.library_path == str(dest)


def test_import_entry_single_file(clean_db, dirs, book):
    (dirs.imports_dir / "The Mayor of Noobtown.m4b").write_bytes(b"audio")
    entry = scan_imports()[0]

    dest = import_entry(clean_db, book, entry)

    assert (dest / "The Mayor of Noobtown.m4b").read_bytes() == b"audio"


def test_import_entry_destination_conflict(clean_db, dirs, book):
    dest = dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    dest.mkdir(parents=True)
    (dest / "existing.mp3").write_bytes(b"x")
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]

    with pytest.raises(ImportFailure, match="destination already exists"):
        import_entry(clean_db, book, entry)
    assert (dirs.imports_dir / "The Mayor of Noobtown").exists()


def test_import_entry_rejects_already_imported_book(clean_db, dirs, book):
    book.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]

    with pytest.raises(ImportFailure, match="already in the library"):
        import_entry(clean_db, book, entry)


# --- routes ----------------------------------------------------------------


@respx.mock
def test_imports_page_identifies_and_caches(client, clean_db, dirs):
    put(dirs, "The Mayor of Noobtown")
    route = hardcover_dispatch([MAYOR_DOC])

    response = client.get("/imports")

    assert response.status_code == 200
    assert "The Mayor of Noobtown" in response.text
    assert "Ryan Rimmel" in response.text
    assert "match" in response.text
    first_calls = route.call_count
    assert first_calls >= 1

    # identification is cached: a rescan doesn't re-hit Hardcover
    response = client.get("/imports")
    assert response.status_code == 200
    assert route.call_count == first_calls


@respx.mock
def test_imports_page_unmatched(client, clean_db, dirs):
    put(dirs, "Nothing Like Anything At All")
    hardcover_dispatch([])

    response = client.get("/imports")

    assert "No match" in response.text


@respx.mock
def test_import_via_route_creates_ownerless_book(client, clean_db, dirs):
    """Importing a book nobody tracks pulls its metadata from Hardcover and
    creates a Book with no shelf membership and no Hardcover mutations."""
    put(dirs, "The Mayor of Noobtown")
    route = hardcover_dispatch(
        [MAYOR_DOC],
        books={646489: book_response(
            646489, "The Mayor of Noobtown", series=("Noobtown", 300, 1.0))},
    )

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "The Mayor of Noobtown",
            "hc__The Mayor of Noobtown": "646489",
        },
    )

    assert response.status_code == 200
    created = clean_db.query(Book).filter_by(hardcover_id=646489).one()
    assert created.editions[0].download_state == DownloadState.IMPORTED
    assert created.editions[0].library_path is not None
    assert created.series.name == "Noobtown"
    # ownerless: no shelf memberships, and no shelving mutation was sent
    assert clean_db.query(UserBook).count() == 0
    for call in route.calls:
        assert b"insert_user_book" not in call.request.content


def test_import_one_via_route_existing_book(client, clean_db, dirs, book):
    put(dirs, "The Mayor of Noobtown")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "The Mayor of Noobtown",
            "hc__The Mayor of Noobtown": str(book.hardcover_id),
        },
    )

    assert response.status_code == 200
    clean_db.refresh(book)
    assert book.editions[0].download_state == DownloadState.IMPORTED
    assert "nothing to review" in response.text


def test_import_all_via_route(client, clean_db, dirs, book, no_background_sync):
    author2 = Author(hardcover_id=501, name="Andy Weir")
    book2 = Book(hardcover_id=700, title="Project Hail Mary", author=author2)
    clean_db.add(book2)
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    put(dirs, "Project Hail Mary")

    response = client.post(
        "/imports/import",
        data={
            "mode": "all",
            "hc__The Mayor of Noobtown": str(book.hardcover_id),
            "hc__Project Hail Mary": str(book2.hardcover_id),
        },
    )

    assert response.status_code == 200
    assert "nothing to review" in response.text
    assert (dirs.library_dir / "Andy Weir" / "Project Hail Mary").is_dir()
    # a successful import kicks the background sync so users who have the
    # book on Hardcover pick it up immediately
    assert no_background_sync


def test_import_without_label_into_available_book_errors(client, clean_db, dirs, book):
    book.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "The Mayor of Noobtown",
            "hc__The Mayor of Noobtown": str(book.hardcover_id),
        },
    )

    assert "give these files an edition label" in response.text
    assert (dirs.imports_dir / "The Mayor of Noobtown").exists()


def test_set_match_renders_manual_row(client, clean_db, dirs, book):
    put(dirs, "Oddly Named Thing")

    response = client.post(
        "/imports/set-match", data={"rel": "Oddly Named Thing", "book_id": str(book.id)}
    )

    assert response.status_code == 200
    assert "manual" in response.text
    assert "The Mayor of Noobtown" in response.text


def test_set_hardcover_match_caches_choice(client, clean_db, dirs):
    put(dirs, "Oddly Named Thing")

    response = client.post(
        "/imports/set-hardcover-match",
        data={
            "rel": "Oddly Named Thing",
            "hardcover_id": "9000",
            "title": "Brand New Book",
            "author": "Somebody New",
            "series_name": "",
        },
    )

    assert response.status_code == 200
    assert "Brand New Book" in response.text
    assert "manual" in response.text
    cached = clean_db.get(AppState, "imports_match:Oddly Named Thing")
    assert cached is not None
    assert '"hardcover_id": 9000' in cached.value


@respx.mock
def test_reidentify_busts_cache(client, clean_db, dirs):
    put(dirs, "The Mayor of Noobtown")
    clean_db.add(AppState(key="imports_match:The Mayor of Noobtown", value="null"))
    clean_db.commit()
    hardcover_dispatch([MAYOR_DOC])

    response = client.post(
        "/imports/reidentify", data={"rel": "The Mayor of Noobtown"}
    )

    assert response.status_code == 200
    assert "Ryan Rimmel" in response.text


def test_match_search_returns_local_options(client, clean_db, dirs, book):
    put(dirs, "Something")

    response = client.get("/imports/search", params={"rel": "Something", "q": "mayor"})

    assert "set-match" in response.text
    assert "The Mayor of Noobtown" in response.text
    assert "Search Hardcover" in response.text


# --- imports as an additional edition ---------------------------------------


def test_import_entry_as_second_edition(clean_db, dirs, book):
    old_dir = dirs.library_dir / "Ryan Rimmel" / "Noobtown {Jim Dale}" / "1 - The Mayor of Noobtown"
    old_dir.mkdir(parents=True)
    (old_dir / "dale.mp3").write_bytes(b"audio")
    book.editions.append(Edition(label="Jim Dale", download_state=DownloadState.IMPORTED,
                                 library_path=str(old_dir)))
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]

    dest = import_entry(clean_db, book, entry, label="Stephen Fry")

    assert dest == (dirs.library_dir / "Ryan Rimmel" / "Noobtown {Stephen Fry}"
                    / "1 - The Mayor of Noobtown")
    assert (dest / "part1.mp3").exists()
    # the Jim Dale edition is untouched
    assert (old_dir / "dale.mp3").exists()
    editions = {e.label: e for e in book.editions}
    assert editions["Stephen Fry"].library_path == str(dest)
    assert editions["Stephen Fry"].download_state == DownloadState.IMPORTED
    assert editions["Jim Dale"].library_path == str(old_dir)


def test_import_entry_duplicate_label_rejected(clean_db, dirs, book):
    book.editions.append(Edition(label="Stephen Fry", download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]

    with pytest.raises(ImportFailure, match='already has its "Stephen Fry" edition'):
        import_entry(clean_db, book, entry, label="Stephen Fry")


def test_import_entry_labelled_requires_labelled_existing(clean_db, dirs, book):
    book.editions.append(Edition(label="", download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]

    with pytest.raises(ImportFailure, match="need a label first"):
        import_entry(clean_db, book, entry, label="Stephen Fry")
    assert (dirs.imports_dir / "The Mayor of Noobtown").exists()


def test_import_route_as_second_edition(client, clean_db, dirs, book, no_background_sync):
    old_dir = dirs.library_dir / "Ryan Rimmel" / "Noobtown {Jim Dale}" / "1 - The Mayor of Noobtown"
    old_dir.mkdir(parents=True)
    (old_dir / "dale.mp3").write_bytes(b"audio")
    book.editions.append(Edition(label="Jim Dale", download_state=DownloadState.IMPORTED,
                                 library_path=str(old_dir)))
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "The Mayor of Noobtown",
            "hc__The Mayor of Noobtown": str(book.hardcover_id),
            "edlabel__The Mayor of Noobtown": "Stephen Fry",
        },
    )

    assert response.status_code == 200
    assert "nothing to review" in response.text
    clean_db.expire_all()
    editions = {e.label: e for e in book.editions}
    assert editions["Stephen Fry"].library_path is not None
    assert "Noobtown {Stephen Fry}" in editions["Stephen Fry"].library_path
    # a successful import still kicks the background all-user sync
    assert no_background_sync


def test_available_books_are_matchable(client, clean_db, dirs, book):
    book.editions.append(Edition(download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    clean_db.commit()
    put(dirs, "Something")

    response = client.get("/imports/search", params={"rel": "Something", "q": "mayor"})

    assert "The Mayor of Noobtown" in response.text
