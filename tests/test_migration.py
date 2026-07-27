"""The Alembic history is a single squashed revision. Upgrading an empty
database to head must produce exactly the schema the models expect, and the
ORM must be able to read and write it."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

REPO = Path(__file__).parent.parent


@pytest.fixture
def migrated(test_settings, tmp_path, monkeypatch):
    """An empty database upgraded to head. Yields an engine on the file."""
    monkeypatch.setattr(test_settings, "config_dir", tmp_path)
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(test_settings.database_url)
    yield engine
    engine.dispose()


def test_history_is_a_single_revision():
    script = ScriptDirectory(str(REPO / "alembic"))
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1
    assert revisions[0].down_revision is None


def test_migrated_schema_matches_the_models(migrated):
    from app.models import Base

    with migrated.connect() as conn:
        context = MigrationContext.configure(conn)
        assert compare_metadata(context, Base.metadata) == []


def test_stamped_at_head(migrated):
    script = ScriptDirectory(str(REPO / "alembic"))
    with migrated.connect() as conn:
        stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert stamped == script.get_current_head()


def test_orm_round_trips_on_the_migrated_schema(migrated):
    from sqlalchemy.orm import Session

    from app.models import Author, Book, DownloadState, Edition, User

    with Session(migrated) as session:
        session.add(User(uuid="u-1", username="dave", password_hash="x", hardcover_token=""))
        author = Author(name="Ryan Rimmel")
        book = Book(hardcover_id=100, title="Imported", author=author)
        session.add(
            Edition(
                book=book,
                download_state=DownloadState.IMPORTED,
                library_path="/audiobooks/Ryan Rimmel/Imported",
            )
        )
        session.commit()

        edition = session.query(Edition).one()
        assert edition.book.title == "Imported"
        assert edition.label == ""
        assert edition.audio_files == []


def test_downgrade_drops_everything(migrated):
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    command.downgrade(cfg, "base")
    with migrated.connect() as conn:
        tables = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'"
        )).scalars().all()
    assert tables == ["alembic_version"]
