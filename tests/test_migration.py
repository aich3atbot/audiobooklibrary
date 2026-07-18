"""The editions migration on a live pre-edition database: every book with
pipeline state gets one unlabelled edition, dependents are repointed from
book_id to edition_id, and nothing else is lost."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

REPO = Path(__file__).parent.parent
PRE_EDITIONS = "91acd8a782ef"


@pytest.fixture
def migrated(test_settings, tmp_path, monkeypatch):
    """A database created at the pre-editions revision, seeded with legacy
    rows, then upgraded to head. Yields an engine on the upgraded file."""
    monkeypatch.setattr(test_settings, "config_dir", tmp_path)
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    command.upgrade(cfg, PRE_EDITIONS)

    engine = create_engine(test_settings.database_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO user (id, uuid, username, password_hash, hardcover_token, enabled)"
            " VALUES (1, 'u-1', 'dave', 'x', '', 1), (2, 'u-2', 'pat', 'x', '', 1)"
        ))
        conn.execute(text("INSERT INTO author (id, name) VALUES (1, 'Ryan Rimmel')"))
        conn.execute(text(
            "INSERT INTO book (id, hardcover_id, title, author_id, download_state, library_path)"
            " VALUES"
            "  (1, 100, 'Imported', 1, 'imported', '/audiobooks/Ryan Rimmel/Imported'),"
            "  (2, 200, 'Downloading', 1, 'grabbed', NULL),"
            "  (3, 300, 'Metadata Only', 1, 'none', NULL),"
            # replace-immediately: no files on disk, but progress rows exist
            "  (4, 400, 'Replacing', 1, 'grabbed', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO release (id, book_id, guid, indexer, title, status, info_hash)"
            " VALUES (1, 2, 'g', 'ABB', 'Downloading [M4B]', 'downloading', 'aa'),"
            "        (2, 4, 'g2', 'ABB', 'Replacing [M4B]', 'grabbed', 'bb')"
        ))
        conn.execute(text(
            "INSERT INTO audio_file (id, book_id, \"index\", rel_path, size, mtime_ms, mime_type)"
            " VALUES (1, 1, 1, 'Part 1.mp3', 10, 0, 'audio/mpeg'),"
            "        (2, 1, 2, 'Part 2.mp3', 10, 0, 'audio/mpeg')"
        ))
        conn.execute(text(
            "INSERT INTO media_progress (id, user_id, book_id, current_time, duration, is_finished)"
            " VALUES (1, 1, 1, 30.0, 100.0, 0), (2, 2, 1, 99.0, 100.0, 1),"
            "        (3, 1, 4, 12.0, 90.0, 0)"
        ))
        conn.execute(text(
            "INSERT INTO bookmark (id, user_id, book_id, time, title)"
            " VALUES (1, 1, 1, 42.0, 'good bit')"
        ))

    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


def rows(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).all()


def test_editions_backfilled_for_books_with_state(migrated):
    editions = rows(
        migrated,
        "SELECT book_id, label, download_state, library_path FROM edition ORDER BY book_id",
    )
    # book 3 (pure metadata) gets no edition; the others one unlabelled each
    assert editions == [
        (1, "", "imported", "/audiobooks/Ryan Rimmel/Imported"),
        (2, "", "grabbed", None),
        (4, "", "grabbed", None),
    ]


def test_book_columns_dropped(migrated):
    cols = [r[1] for r in rows(migrated, "PRAGMA table_info(book)")]
    assert "download_state" not in cols
    assert "library_path" not in cols
    assert rows(migrated, "SELECT title FROM book WHERE id = 3") == [("Metadata Only",)]


def test_dependents_repointed_to_editions(migrated):
    def edition_of(book_id):
        return rows(migrated, f"SELECT id FROM edition WHERE book_id = {book_id}")[0][0]

    assert rows(migrated, "SELECT id, edition_id FROM release ORDER BY id") == [
        (1, edition_of(2)),
        (2, edition_of(4)),
    ]
    assert rows(migrated, "SELECT id, edition_id FROM audio_file ORDER BY id") == [
        (1, edition_of(1)),
        (2, edition_of(1)),
    ]
    assert rows(
        migrated,
        'SELECT id, user_id, edition_id, "current_time" FROM media_progress ORDER BY id',
    ) == [
        (1, 1, edition_of(1), 30.0),
        (2, 2, edition_of(1), 99.0),
        (3, 1, edition_of(4), 12.0),
    ]
    assert rows(migrated, "SELECT id, edition_id, title FROM bookmark") == [
        (1, edition_of(1), "good bit")
    ]


def test_progress_unique_per_user_and_edition(migrated):
    edition_id = rows(migrated, "SELECT id FROM edition WHERE book_id = 1")[0][0]
    import sqlite3

    with pytest.raises(Exception) as excinfo:
        with migrated.begin() as conn:
            conn.execute(text(
                "INSERT INTO media_progress (user_id, edition_id, current_time, duration)"
                f" VALUES (1, {edition_id}, 0, 0)"
            ))
    assert "UNIQUE" in str(excinfo.value)


def test_orm_reads_the_upgraded_schema(migrated, test_settings):
    """The models must load rows from the migrated file as-is."""
    from sqlalchemy.orm import Session

    from app.models import Book, Edition

    with Session(migrated) as session:
        edition = session.query(Edition).filter(Edition.library_path.is_not(None)).one()
        assert edition.book.title == "Imported"
        assert edition.label == ""
        assert [f.rel_path for f in edition.audio_files] == ["Part 1.mp3", "Part 2.mp3"]
        assert {p.user_id for p in edition.media_progress} == {1, 2}
        book = session.get(Book, 3)
        assert book.editions == []
