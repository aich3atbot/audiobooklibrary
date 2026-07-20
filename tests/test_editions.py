"""Edition folder layout and the relabel/rename engine."""

import pytest

from app.models import (
    AppState,
    AudioFile,
    Author,
    Book,
    DownloadState,
    Edition,
    Release,
    Series,
    UserBook,
)
from app.services.editions import (
    get_or_create_edition,
    relabel_edition,
    series_edition_candidates,
    suggest_labels,
)
from app.services.importer import ImportFailure, edition_dir_for


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, AudioFile, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def clean_dirs(test_settings):
    import shutil

    for d in (test_settings.download_dir, test_settings.library_dir):
        if d.exists():
            for child in d.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        else:
            d.mkdir(parents=True)
    return test_settings


@pytest.fixture
def rowling(clean_db):
    author = Author(hardcover_id=500, name="J.K. Rowling")
    clean_db.add(author)
    return author


def make_book(db, author, title, series=None, index=None, hardcover_id=None):
    book = Book(
        hardcover_id=hardcover_id or abs(hash(title)) % 10**6,
        title=title,
        author=author,
        series=series,
        series_index=index,
    )
    db.add(book)
    db.commit()
    return book


def make_edition(db, book, label="", library_path=None, **kwargs):
    edition = Edition(book=book, label=label, library_path=library_path, **kwargs)
    db.add(edition)
    db.commit()
    return edition


def place_files(path, names=("Part 1.mp3",)):
    path.mkdir(parents=True)
    for name in names:
        (path / name).write_bytes(b"audio")
    return path


# --- layout matrix ----------------------------------------------------------


def test_layout_series_unlabelled(clean_db, clean_dirs, rowling):
    series = Series(hardcover_id=300, name="Harry Potter")
    book = make_book(clean_db, rowling, "Harry Potter and the Chamber of Secrets",
                     series=series, index=2.0)
    edition = make_edition(clean_db, book)
    assert edition_dir_for(edition) == (
        clean_dirs.library_dir / "J.K. Rowling" / "Harry Potter"
        / "2 - Harry Potter and the Chamber of Secrets"
    )


def test_layout_series_labelled_suffixes_series_folder(clean_db, clean_dirs, rowling):
    series = Series(hardcover_id=300, name="Harry Potter")
    book = make_book(clean_db, rowling, "Harry Potter and the Chamber of Secrets",
                     series=series, index=2.0)
    edition = make_edition(clean_db, book, label="Stephen Fry")
    assert edition_dir_for(edition) == (
        clean_dirs.library_dir / "J.K. Rowling" / "Harry Potter {Stephen Fry}"
        / "2 - Harry Potter and the Chamber of Secrets"
    )


def test_layout_standalone_labelled_suffixes_book_folder(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "The Casual Vacancy")
    edition = make_edition(clean_db, book, label="Tom Hollander")
    assert edition_dir_for(edition) == (
        clean_dirs.library_dir / "J.K. Rowling" / "The Casual Vacancy {Tom Hollander}"
    )


def test_layout_series_without_index(clean_db, clean_dirs, rowling):
    series = Series(hardcover_id=301, name="Cormoran Strike")
    book = make_book(clean_db, rowling, "The Cuckoo's Calling", series=series)
    edition = make_edition(clean_db, book, label="Full Cast")
    assert edition_dir_for(edition) == (
        clean_dirs.library_dir / "J.K. Rowling" / "Cormoran Strike {Full Cast}"
        / "The Cuckoo's Calling"
    )


def test_layout_label_is_sanitized_inside_component(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    edition = make_edition(clean_db, book, label='Fry/Dale: "the duo"')
    path = edition_dir_for(edition)
    assert path.parent.name == "J.K. Rowling"  # the slash never made a directory
    assert path.name.startswith("Standalone {Fry Dale")
    assert not set('/:*?"<>|') & set(path.name)


def test_layout_component_cap_covers_the_label(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "T" * 140)
    edition = make_edition(clean_db, book, label="A Very Long Narrator Name Indeed")
    assert len(edition_dir_for(edition).name) <= 150


# --- relabel engine ---------------------------------------------------------


def test_relabel_without_files_just_sets_label(clean_db, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    edition = make_edition(clean_db, book)
    assert relabel_edition(clean_db, edition, "Stephen Fry") is None
    assert edition.label == "Stephen Fry"


def test_relabel_moves_folder_and_keeps_rel_paths(clean_db, clean_dirs, rowling):
    series = Series(hardcover_id=300, name="Harry Potter")
    book = make_book(clean_db, rowling, "Chamber of Secrets", series=series, index=2.0)
    old_dir = place_files(
        clean_dirs.library_dir / "J.K. Rowling" / "Harry Potter" / "2 - Chamber of Secrets",
        names=("Part 1.mp3", "cover.jpg"),
    )
    edition = make_edition(clean_db, book, library_path=str(old_dir),
                           download_state=DownloadState.IMPORTED)
    edition.audio_files.append(
        AudioFile(index=1, rel_path="Part 1.mp3", size=5, mtime_ms=0, mime_type="audio/mpeg")
    )
    clean_db.commit()
    file_id = edition.audio_files[0].id

    new_dir = relabel_edition(clean_db, edition, "Stephen Fry")

    expected = (clean_dirs.library_dir / "J.K. Rowling" / "Harry Potter {Stephen Fry}"
                / "2 - Chamber of Secrets")
    assert new_dir == expected
    assert (expected / "Part 1.mp3").exists()
    assert (expected / "cover.jpg").exists()
    assert not old_dir.exists()
    # the now-empty old series folder is cleaned up
    assert not old_dir.parent.exists()
    assert edition.library_path == str(expected)
    # AudioFile rows (the ABS file inos) survive untouched
    assert edition.audio_files[0].id == file_id
    assert edition.audio_files[0].rel_path == "Part 1.mp3"


def test_relabel_to_unlabelled_moves_back(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    old_dir = place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone {Fry}")
    edition = make_edition(clean_db, book, label="Fry", library_path=str(old_dir))

    new_dir = relabel_edition(clean_db, edition, "")

    assert new_dir == clean_dirs.library_dir / "J.K. Rowling" / "Standalone"
    assert (new_dir / "Part 1.mp3").exists()
    assert edition.label == ""


def test_relabel_same_label_is_a_noop(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    old_dir = place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone {Fry}")
    edition = make_edition(clean_db, book, label="Fry", library_path=str(old_dir))

    assert relabel_edition(clean_db, edition, "Fry") == old_dir
    assert old_dir.exists()


def test_relabel_rejects_duplicate_label(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    make_edition(clean_db, book, label="Fry")
    edition = make_edition(clean_db, book, label="Dale")

    with pytest.raises(ImportFailure, match='already has a "Fry" edition'):
        relabel_edition(clean_db, edition, "Fry")
    assert edition.label == "Dale"


def test_relabel_refuses_occupied_destination(clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    old_dir = place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone")
    place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone {Fry}",
                names=("other.mp3",))
    edition = make_edition(clean_db, book, library_path=str(old_dir))

    with pytest.raises(ImportFailure, match="destination already exists"):
        relabel_edition(clean_db, edition, "Fry")

    clean_db.refresh(edition)
    assert edition.label == ""  # label change was rolled back with the failure
    assert edition.library_path == str(old_dir)
    assert (old_dir / "Part 1.mp3").exists()


def test_relabel_moves_only_that_books_folder_in_a_series(clean_db, clean_dirs, rowling):
    series = Series(hardcover_id=300, name="Harry Potter")
    stone = make_book(clean_db, rowling, "Philosopher's Stone", series=series, index=1.0)
    chamber = make_book(clean_db, rowling, "Chamber of Secrets", series=series, index=2.0)
    series_dir = clean_dirs.library_dir / "J.K. Rowling" / "Harry Potter"
    stone_dir = place_files(series_dir / "1 - Philosopher's Stone")
    chamber_dir = place_files(series_dir / "2 - Chamber of Secrets")
    make_edition(clean_db, stone, library_path=str(stone_dir))
    chamber_edition = make_edition(clean_db, chamber, library_path=str(chamber_dir))

    relabel_edition(clean_db, chamber_edition, "Stephen Fry")

    # only Chamber moved into the labelled series folder; Stone stayed put
    assert (clean_dirs.library_dir / "J.K. Rowling" / "Harry Potter {Stephen Fry}"
            / "2 - Chamber of Secrets" / "Part 1.mp3").exists()
    assert (stone_dir / "Part 1.mp3").exists()
    assert not chamber_dir.exists()


# --- get_or_create / suggestions -------------------------------------------


def test_get_or_create_reuses_label_and_backfills_identity(clean_db, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    first = get_or_create_edition(clean_db, book, "Fry")
    clean_db.commit()
    again = get_or_create_edition(
        clean_db, book, "Fry", hardcover_edition_id=42, narrator="Stephen Fry"
    )
    clean_db.commit()

    assert again is first
    assert first.hardcover_edition_id == 42
    assert first.narrator == "Stephen Fry"
    assert len(book.editions) == 1


def test_suggest_labels_from_the_series(clean_db, rowling):
    series = Series(hardcover_id=300, name="Harry Potter")
    stone = make_book(clean_db, rowling, "Philosopher's Stone", series=series, index=1.0)
    chamber = make_book(clean_db, rowling, "Chamber of Secrets", series=series, index=2.0)
    other = make_book(clean_db, rowling, "The Casual Vacancy")
    make_edition(clean_db, stone, label="Stephen Fry")
    make_edition(clean_db, stone, label="Full Cast")
    make_edition(clean_db, stone, label="")  # unlabelled never suggested
    make_edition(clean_db, other, label="Tom Hollander")  # different series

    assert suggest_labels(clean_db, chamber) == ["Full Cast", "Stephen Fry"]
    assert suggest_labels(clean_db, other) == ["Tom Hollander"]


def test_series_edition_candidates_excludes_own_labels_and_carries_narrator(
    clean_db, rowling
):
    series = Series(hardcover_id=300, name="Harry Potter")
    stone = make_book(clean_db, rowling, "Philosopher's Stone", series=series, index=1.0)
    goblet = make_book(clean_db, rowling, "Goblet of Fire", series=series, index=4.0)
    other = make_book(clean_db, rowling, "The Casual Vacancy")
    make_edition(clean_db, stone, label="Stephen Fry", narrator="Stephen Fry")
    make_edition(clean_db, stone, label="Full Cast")
    make_edition(clean_db, stone, label="")  # unlabelled never offered
    make_edition(clean_db, other, label="Tom Hollander")  # different series
    make_edition(clean_db, goblet, label="Full Cast")  # already on this book

    assert series_edition_candidates(clean_db, goblet) == [
        {"label": "Stephen Fry", "narrator": "Stephen Fry"}
    ]


def test_series_edition_candidates_standalone_book_is_empty(clean_db, rowling):
    book = make_book(clean_db, rowling, "The Casual Vacancy")
    make_edition(clean_db, book, label="Tom Hollander")

    assert series_edition_candidates(clean_db, book) == []


# --- the rename route -------------------------------------------------------


def test_relabel_route_moves_folder_and_rerenders(client, clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    old_dir = place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone")
    edition = make_edition(clean_db, book, library_path=str(old_dir),
                           download_state=DownloadState.IMPORTED)

    response = client.post(f"/editions/{edition.id}/label", data={"label": "Stephen Fry"})

    assert response.status_code == 200
    assert 'id="editions"' in response.text  # the detail page's editions fragment
    assert "Standalone {Stephen Fry}" in response.text
    assert (clean_dirs.library_dir / "J.K. Rowling" / "Standalone {Stephen Fry}"
            / "Part 1.mp3").exists()
    clean_db.expire_all()
    assert edition.label == "Stephen Fry"


def test_relabel_route_shows_conflict_error(client, clean_db, clean_dirs, rowling):
    book = make_book(clean_db, rowling, "Standalone")
    old_dir = place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone")
    place_files(clean_dirs.library_dir / "J.K. Rowling" / "Standalone {Fry}",
                names=("other.mp3",))
    edition = make_edition(clean_db, book, library_path=str(old_dir),
                           download_state=DownloadState.IMPORTED)

    response = client.post(f"/editions/{edition.id}/label", data={"label": "Fry"})

    assert response.status_code == 200
    assert "destination already exists" in response.text
    assert (old_dir / "Part 1.mp3").exists()


def test_relabel_route_unknown_edition_404(client, clean_db):
    assert client.post("/editions/99999/label", data={"label": "X"}).status_code == 404
