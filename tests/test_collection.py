import json
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient
from app.models import AppState, Author, Book, DownloadState, Edition, Release, Series, UserBook
from app.services.collection import (
    annotate_edition_hints,
    cache_match,
    entry_duration,
    entry_for,
    hardcover_query,
    identify_entry,
    import_entry,
    match_from_book,
    scan_imports,
    score_result,
)
from app.services.editions import edition_choice
from app.services.importer import ImportFailure
from tests.test_audio_meta import FRAME_SECONDS, write_mp3

# One Hardcover audiobook edition row, as the editions query returns it.
HC_EDITION = {
    "id": 11,
    "title": "The Mayor of Noobtown",
    "subtitle": None,
    "edition_format": "Audiobook",
    "asin": None,
    "isbn_13": None,
    "audio_seconds": 35000,
    "release_date": "2019-11-20",
    "users_count": 5,
    "publisher": {"name": "Podium"},
    "contributions": [{"contribution": "Narrator", "author": {"id": 1, "name": "Stephen Fry"}}],
}


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
    "slug": "the-mayor-of-noobtown",
    "title": "The Mayor of Noobtown",
    "authors": ["Ryan Rimmel"],
    "series_name": "Noobtown",
    "series_slug": "noobtown",
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
        "slug": parsed.get("slug"),
        "title": parsed["title"],
        "author_names": parsed["authors"],
        "contribution_types": ["Author"] * len(parsed["authors"]),
        "contributions": [
            {"author": {"id": 1, "name": name}} for name in parsed["authors"]
        ],
        "featured_series": (
            {"position": parsed["series_position"],
             "series": {"id": 2, "name": parsed["series_name"],
                        "slug": parsed.get("series_slug")}}
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
def test_imports_page_links_match_to_hardcover(client, clean_db, dirs):
    put(dirs, "The Mayor of Noobtown")
    hardcover_dispatch([MAYOR_DOC])

    response = client.get("/imports")

    # badges, not inline links: the title and series lines stay plain text
    assert '<strong>The Mayor of Noobtown — Ryan Rimmel</strong>' in response.text
    assert 'class="match-badges"' in response.text
    assert ('<a class="badge" href="https://hardcover.app/books/the-mayor-of-noobtown"'
            in response.text)
    assert '<a class="badge" href="https://hardcover.app/series/noobtown"' in response.text
    assert 'target="_blank"' in response.text
    # the header's select-all toggle must not be posted with the row selection
    assert "Select all matched" in response.text


@respx.mock
def test_stale_cached_match_is_reidentified_for_its_slug(client, clean_db, dirs):
    """Matches cached before we recorded slugs carry no link. An
    auto-identified one is re-identified once to pick the slug up."""
    put(dirs, "The Mayor of Noobtown")
    clean_db.add(AppState(
        key="imports_match:The Mayor of Noobtown",
        value=json.dumps({"hardcover_id": 646489, "title": "The Mayor of Noobtown",
                          "author": "Ryan Rimmel", "series_name": "Noobtown",
                          "series_position": 1.0, "score": 0.9}),
    ))
    clean_db.commit()
    route = hardcover_dispatch([MAYOR_DOC])

    response = client.get("/imports")

    assert 'href="https://hardcover.app/books/the-mayor-of-noobtown"' in response.text
    # re-cached in the current shape: the next load is a cache hit again
    calls = route.call_count
    assert client.get("/imports").status_code == 200
    assert route.call_count == calls


def test_stale_manual_match_takes_slugs_from_the_local_book(client, clean_db, dirs, book):
    """A manual match is the user's own choice, so it is never silently
    re-identified — its slugs come from the local book row instead."""
    book.hardcover_slug = "the-mayor-of-noobtown"
    book.series.hardcover_slug = "noobtown"
    clean_db.commit()
    put(dirs, "Oddly Named Thing")
    clean_db.add(AppState(
        key="imports_match:Oddly Named Thing",
        value=json.dumps({"hardcover_id": 646489, "title": "The Mayor of Noobtown",
                          "author": "Ryan Rimmel", "series_name": "Noobtown",
                          "series_position": 1.0, "score": None}),
    ))
    clean_db.commit()

    # no respx mock: re-identifying would raise on the unmocked request
    response = client.get("/imports")

    assert 'href="https://hardcover.app/books/the-mayor-of-noobtown"' in response.text
    assert 'href="https://hardcover.app/series/noobtown"' in response.text


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


@respx.mock
def test_picker_suggests_series_labels_but_not_imported_ones(client, clean_db, dirs, book):
    book.editions.append(Edition(label="Jim Dale", download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    sibling = Book(hardcover_id=646490, title="Noobtown Two", author=book.author,
                   series=book.series, series_index=2.0)
    sibling.editions.append(Edition(label="Stephen Fry"))
    clean_db.add(sibling)
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    cache_match(clean_db, "The Mayor of Noobtown", match_from_book(book))
    respx.post(API_URL).mock(return_value=httpx.Response(200, json={"data": {"editions": []}}))

    response = client.get("/imports/editions", params={"rel": "The Mayor of Noobtown"})

    assert response.status_code == 200
    assert "datalist" in response.text
    assert 'value="Stephen Fry"' in response.text  # the series-mate's label
    # already imported here, so it can only be reached through "replace"
    assert '<option value="Jim Dale">' not in response.text


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
            "edition_label__The Mayor of Noobtown": "Stephen Fry",
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


def test_local_match_options_name_the_series_position(client, clean_db, dirs, book):
    put(dirs, "Something")

    response = client.get("/imports/search", params={"rel": "Something", "q": "mayor"})

    # the position is what tells two books of one series apart in this list
    assert "(Noobtown #1)" in response.text


@respx.mock
def test_hardcover_match_options_name_the_series_position(client, clean_db, dirs):
    put(dirs, "Something")
    hardcover_dispatch([MAYOR_DOC])

    response = client.get("/imports/hardcover", params={"rel": "Something", "q": "mayor"})

    assert "The Mayor of Noobtown — Ryan Rimmel" in response.text
    assert "(Noobtown #1)" in response.text


# --- choosing an edition at import time -------------------------------------


def test_entry_duration_sums_every_audio_file(dirs):
    put(dirs, "Timed", files=())
    expected = sum(write_mp3(dirs.imports_dir / "Timed" / f, frames=n)
                   for f, n in (("part1.mp3", 100), ("part2.mp3", 50)))
    # a stray non-audio file must not upset the total
    (dirs.imports_dir / "Timed" / "cover.jpg").write_bytes(b"not audio")

    # mutagen estimates length from the bitrate, so allow a little slack
    assert entry_duration(entry_for("Timed")) == pytest.approx(expected, rel=0.01)


def test_entry_duration_is_none_when_nothing_parses(dirs):
    put(dirs, "Junk")  # 10 bytes of "x" per file

    assert entry_duration(entry_for("Junk")) is None


def test_annotate_edition_hints_flags_closest_duration_and_narrator(dirs):
    put(dirs, "Ryan Rimmel/Noobtown/Stephen Fry - The Mayor of Noobtown")
    entry = entry_for("Ryan Rimmel/Noobtown/Stephen Fry - The Mayor of Noobtown")
    editions = [
        {"id": 1, "narrators": ["Jim Dale"], "audio_seconds": 36000},
        {"id": 2, "narrators": ["Stephen Fry"], "audio_seconds": 35500},
        {"id": 3, "narrators": ["Rufus Beck"], "audio_seconds": 35200},
        {"id": 4, "narrators": ["Ann Saunders"], "audio_seconds": None},
    ]

    annotate_edition_hints(editions, entry, 35000)

    # closest within tolerance wins, and only it — 36000 is outside 5%
    assert [e["hint_duration"] for e in editions] == [False, False, True, False]
    # the narrator named in the folder path is flagged wherever it lands
    assert [e["hint_narrator"] for e in editions] == [False, True, False, False]


def test_annotate_edition_hints_without_duration_still_matches_narrator(dirs):
    put(dirs, "The Mayor of Noobtown")
    entry = entry_for("The Mayor of Noobtown")
    editions = [{"id": 1, "narrators": ["Stephen Fry"], "audio_seconds": 35000}]

    annotate_edition_hints(editions, entry, None)

    assert editions[0]["hint_duration"] is False
    assert editions[0]["hint_narrator"] is False


def test_edition_choice_reads_only_its_own_rows_fields():
    form = {
        "hc_edition__a/one": "11",
        "hclabel_11__a/one": "Stephen Fry",
        "hcnarr_11__a/one": "Stephen Fry",
        "hc_edition__b/two": "22",
        "hclabel_22__b/two": "Jim Dale",
        "hcnarr_22__b/two": "Jim Dale",
        "edition_label__b/two": "Typed Override",
    }

    assert edition_choice(form, ns="a/one") == ("Stephen Fry", 11, "Stephen Fry")
    # a typed label beats the picked edition's default, per row
    assert edition_choice(form, ns="b/two") == ("Typed Override", 22, "Jim Dale")
    # an untouched row picks up nothing at all
    assert edition_choice(form, ns="c/three") == ("", None, "")


@respx.mock
def test_edition_picker_namespaces_fields_and_badges_the_match(client, clean_db, dirs, book):
    put(dirs, "Mayor", files=())
    write_mp3(dirs.imports_dir / "Mayor" / "book.mp3", frames=1000)
    seconds = 1000 * FRAME_SECONDS
    cache_match(clean_db, "Mayor", match_from_book(book))
    respx.post(API_URL).mock(return_value=httpx.Response(200, json={"data": {"editions": [
        HC_EDITION | {"id": 11, "audio_seconds": int(seconds)},
        HC_EDITION | {"id": 22, "audio_seconds": int(seconds) * 3, "contributions": [
            {"contribution": "Narrator", "author": {"id": 2, "name": "Jim Dale"}}]},
    ]}}))

    response = client.get("/imports/editions", params={"rel": "Mayor"})

    assert response.status_code == 200
    # fields carry the entry's rel so sibling rows in the table can't collide
    assert 'name="hc_edition__Mayor"' in response.text
    assert 'value="Stephen Fry"' in response.text
    # the label box is the picker's own first option, not a row-level field
    assert 'name="edition_label__Mayor"' in response.text
    assert 'value="custom"' in response.text
    # only the runtime-matching edition is badged, and nothing is preselected
    assert response.text.count("≈ your files") == 1
    # no radio carries a checked attribute (the custom option's oninput script
    # mentions .checked, hence the leading space)
    assert " checked" not in response.text


def test_edition_picker_without_a_match_asks_for_one_first(client, dirs):
    put(dirs, "Unknown")

    response = client.get("/imports/editions", params={"rel": "Unknown"})

    assert response.status_code == 200
    assert "Match this entry to a book first." in response.text


def test_edition_picker_404s_for_a_vanished_entry(client, dirs):
    assert client.get("/imports/editions", params={"rel": "Gone"}).status_code == 404


@respx.mock
def test_import_records_the_picked_hardcover_edition(client, clean_db, dirs, book):
    put(dirs, "Mayor")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "Mayor",
            "hc__Mayor": str(book.hardcover_id),
            "hc_edition__Mayor": "11",
            "hclabel_11__Mayor": "Stephen Fry",
            "hcnarr_11__Mayor": "Stephen Fry",
        },
    )

    assert response.status_code == 200
    clean_db.expire_all()
    edition = book.editions[0]
    assert edition.label == "Stephen Fry"
    assert edition.hardcover_edition_id == 11
    assert edition.narrator == "Stephen Fry"
    assert "Noobtown {Stephen Fry}" in edition.library_path


@respx.mock
def test_import_with_an_empty_own_label_keeps_the_plain_folder(client, clean_db, dirs, book):
    put(dirs, "Mayor")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "Mayor",
            "hc__Mayor": str(book.hardcover_id),
            # the "my own label" option, left empty
            "hc_edition__Mayor": "custom",
            "edition_label__Mayor": "",
        },
    )

    assert response.status_code == 200
    clean_db.expire_all()
    edition = book.editions[0]
    assert edition.label == ""
    assert "{" not in edition.library_path


@respx.mock
def test_a_selected_edition_beats_text_left_in_the_label_box(client, clean_db, dirs, book):
    put(dirs, "Mayor")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "Mayor",
            "hc__Mayor": str(book.hardcover_id),
            "hc_edition__Mayor": "11",
            "hclabel_11__Mayor": "Stephen Fry",
            "hcnarr_11__Mayor": "Stephen Fry",
            # typed first, then a Hardcover edition was picked instead: the
            # radio decides, unlike the download pickers' override box
            "edition_label__Mayor": "Changed My Mind",
        },
    )

    assert response.status_code == 200
    clean_db.expire_all()
    assert book.editions[0].label == "Stephen Fry"


@respx.mock
def test_import_replaces_an_existing_editions_files(client, clean_db, dirs, book):
    old_dir = dirs.library_dir / "Ryan Rimmel" / "Noobtown {Jim Dale}" / "1 - The Mayor of Noobtown"
    old_dir.mkdir(parents=True)
    (old_dir / "old.mp3").write_bytes(b"stale")
    existing = Edition(label="Jim Dale", download_state=DownloadState.IMPORTED,
                       library_path=str(old_dir))
    book.editions.append(existing)
    clean_db.commit()
    edition_id = existing.id
    put(dirs, "Mayor", files=("new.mp3",))

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "Mayor",
            "hc__Mayor": str(book.hardcover_id),
            "hc_edition__Mayor": f"rep_{edition_id}",
            "hclabel_rep_{}__Mayor".format(edition_id): "Jim Dale",
        },
    )

    assert response.status_code == 200
    clean_db.expire_all()
    assert len(book.editions) == 1  # replaced in place, not added alongside
    edition = book.editions[0]
    assert edition.id == edition_id
    assert edition.label == "Jim Dale"
    assert Path(edition.library_path) == old_dir
    # the stale files are gone and the imported ones took their place
    assert not (old_dir / "old.mp3").exists()
    assert (old_dir / "new.mp3").exists()
    assert not (dirs.imports_dir / "Mayor").exists()


@respx.mock
def test_import_refuses_to_replace_a_vanished_edition(client, clean_db, dirs, book):
    book.editions.append(Edition(label="Jim Dale", download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/gone"))
    clean_db.commit()
    put(dirs, "Mayor")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "Mayor",
            "hc__Mayor": str(book.hardcover_id),
            "hc_edition__Mayor": "rep_9999",
        },
    )

    assert response.status_code == 200
    assert "no longer exists" in response.text
    assert (dirs.imports_dir / "Mayor").exists()  # nothing moved


@respx.mock
def test_edition_picker_offers_this_books_editions_as_replacements(
    client, clean_db, dirs, book
):
    book.editions.append(Edition(label="Jim Dale", narrator="Jim Dale",
                                 download_state=DownloadState.IMPORTED,
                                 library_path="/audiobooks/somewhere"))
    clean_db.commit()
    edition_id = book.editions[0].id
    put(dirs, "Mayor")
    cache_match(clean_db, "Mayor", match_from_book(book))
    respx.post(API_URL).mock(return_value=httpx.Response(
        200, json={"data": {"editions": [HC_EDITION]}}))

    response = client.get("/imports/editions", params={"rel": "Mayor"})

    assert response.status_code == 200
    assert f'value="rep_{edition_id}"' in response.text
    assert "(replace)" in response.text
    # picking one confirms before anything can be deleted
    assert "abEditionReplace" in response.text
