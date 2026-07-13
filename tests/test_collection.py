import shutil

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL
from app.models import AppState, Author, Book, DownloadState, Release, Series, UserBook
from app.services.collection import (
    entry_for,
    import_entry,
    scan_imports,
    score_match,
)
from app.services.importer import ImportFailure
from tests.test_sync import entry as hc_entry
from tests.test_sync import me_response


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Book, Author, Series, AppState):
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


def put(dirs, rel, files=("part1.mp3", "part2.mp3")):
    folder = dirs.imports_dir / rel
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / f).write_bytes(b"x" * 10)
    return folder


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


# --- matcher ---------------------------------------------------------------


def test_score_match_exact_title(dirs, book):
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]
    assert score_match(entry, book) > 0.9


def test_score_match_with_noise_and_author_path(dirs, book):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown [M4B 64kbps]")
    entry = scan_imports()[0]
    assert score_match(entry, book) >= 0.75


def test_score_match_unrelated_is_low(dirs, book):
    put(dirs, "A Completely Different Story by Nobody")
    entry = scan_imports()[0]
    assert score_match(entry, book) < 0.5


# --- import ----------------------------------------------------------------


def test_import_entry_moves_folder_and_cleans_up(clean_db, dirs, book):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown")
    entry = scan_imports()[0]

    dest = import_entry(clean_db, book, entry)

    assert dest == dirs.library_dir / "Ryan Rimmel" / "Noobtown" / "1 - The Mayor of Noobtown"
    assert sorted(p.name for p in dest.iterdir()) == ["part1.mp3", "part2.mp3"]
    # source folder and now-empty parents are gone; imports root remains
    assert not (dirs.imports_dir / "Ryan Rimmel").exists()
    assert dirs.imports_dir.exists()
    assert book.download_state == DownloadState.IMPORTED
    assert book.library_path == str(dest)


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
    book.library_path = "/audiobooks/somewhere"
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")
    entry = scan_imports()[0]

    with pytest.raises(ImportFailure, match="already in the library"):
        import_entry(clean_db, book, entry)


# --- routes ----------------------------------------------------------------


def test_imports_page_matches_book(client, clean_db, dirs, book):
    put(dirs, "Ryan Rimmel/Noobtown/01 - The Mayor of Noobtown")

    response = client.get("/imports")

    assert response.status_code == 200
    assert "The Mayor of Noobtown" in response.text
    assert "match" in response.text
    assert "Import all matched" in response.text


def test_imports_page_unmatched(client, clean_db, dirs, book):
    put(dirs, "Nothing Like Anything")

    response = client.get("/imports")

    assert "No match" in response.text


def test_import_one_via_route(client, clean_db, dirs, book):
    put(dirs, "The Mayor of Noobtown")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "The Mayor of Noobtown",
            "book__The Mayor of Noobtown": str(book.id),
        },
    )

    assert response.status_code == 200
    clean_db.refresh(book)
    assert book.download_state == DownloadState.IMPORTED
    assert "nothing to review" in response.text


def test_import_all_via_route(client, clean_db, dirs, book):
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
            "book__The Mayor of Noobtown": str(book.id),
            "book__Project Hail Mary": str(book2.id),
        },
    )

    assert response.status_code == 200
    assert "nothing to review" in response.text
    assert (dirs.library_dir / "Andy Weir" / "Project Hail Mary").is_dir()


def test_import_error_reported_in_row(client, clean_db, dirs, book):
    book.library_path = "/audiobooks/somewhere"
    clean_db.commit()
    put(dirs, "The Mayor of Noobtown")

    response = client.post(
        "/imports/import",
        data={
            "mode": "one",
            "rel": "The Mayor of Noobtown",
            "book__The Mayor of Noobtown": str(book.id),
        },
    )

    assert "already in the library" in response.text
    assert (dirs.imports_dir / "The Mayor of Noobtown").exists()


def test_set_match_renders_manual_row(client, clean_db, dirs, book):
    put(dirs, "Oddly Named Thing")

    response = client.post(
        "/imports/set-match", data={"rel": "Oddly Named Thing", "book_id": str(book.id)}
    )

    assert response.status_code == 200
    assert "manual" in response.text
    assert "The Mayor of Noobtown" in response.text


def test_match_search_returns_local_options(client, clean_db, dirs, book):
    put(dirs, "Something")

    response = client.get("/imports/search", params={"rel": "Something", "q": "mayor"})

    assert "set-match" in response.text
    assert "The Mayor of Noobtown" in response.text
    assert "Search Hardcover" in response.text


@respx.mock
def test_add_match_adds_from_hardcover(client, clean_db, dirs):
    put(dirs, "Brand New Book")
    respx.post(API_URL).side_effect = [
        httpx.Response(200, json={"data": {"insert_user_book": {"id": 42, "error": None}}}),
        me_response([hc_entry(ub_id=42, status_id=3, book_id=9000, title="Brand New Book")]),
    ]

    response = client.post(
        "/imports/add-match",
        data={"rel": "Brand New Book", "hardcover_id": "9000", "state": "read"},
    )

    assert response.status_code == 200
    assert "Brand New Book" in response.text
    assert "manual" in response.text
    assert clean_db.query(Book).filter_by(hardcover_id=9000).count() == 1
