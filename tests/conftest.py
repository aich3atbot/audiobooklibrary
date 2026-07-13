import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(scope="session", autouse=True)
def test_settings(tmp_path_factory):
    """Point all paths at temp dirs before the app is imported anywhere."""
    root = tmp_path_factory.mktemp("abl")
    os.environ["CONFIG_DIR"] = str(root / "config")
    os.environ["DOWNLOAD_DIR"] = str(root / "downloads")
    os.environ["LIBRARY_DIR"] = str(root / "audiobooks")

    from app.config import get_settings
    from app.db import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    from app.models import Base

    Base.metadata.create_all(get_engine())
    yield get_settings()


@pytest.fixture
def client(test_settings):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def db_session(test_settings):
    from app.db import get_sessionmaker

    with get_sessionmaker()() as session:
        yield session
