"""Settings loading: an empty environment value must fall back to the default.

docker-compose passes optional variables through bare, so an unset one never
reaches the container — but a variable left empty in .env still arrives as "",
and parsing that as an int or a bool used to abort startup.
"""

from pathlib import Path

import pytest

from app.config import Settings

# Every optional variable, emptied. OPTIONAL_BLANK is what the container saw
# before this was fixed: docker-compose expanded ${VAR:-} to an empty string.
OPTIONAL_BLANK = {
    "DOWNLOAD_REMOVE_IMMEDIATELY": "",
    "SYNC_INTERVAL_MINUTES": "",
    "WATCH_INTERVAL_SECONDS": "",
    "DOWNLOAD_QUIET_SECONDS": "",
    "IMPORT_MODE": "",
    "MARK_READING_AFTER_MINUTES": "",
    "MARK_READ_TAIL_MINUTES": "",
    "CONFIG_DIR": "",
    "DOWNLOAD_DIR": "",
    "LIBRARY_DIR": "",
    "IMPORTS_DIR": "",
    "DOWNLOAD_LABEL": "",
    "DOWNLOAD_CLIENT": "",
    "DOWNLOAD_URL": "",
    "INDEX_URL": "",
}


def load() -> Settings:
    """Build settings from the environment alone, ignoring the developer's .env."""
    return Settings(_env_file=None)


@pytest.fixture
def clean_env(monkeypatch):
    """Only the variables a test sets are visible."""
    for key in OPTIONAL_BLANK:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")


def test_unset_optionals_use_defaults(clean_env):
    settings = load()
    assert settings.sync_interval_minutes == 30
    assert settings.import_mode == "copy"
    assert settings.config_dir == Path("/config")


def test_blank_optionals_use_defaults(clean_env, monkeypatch):
    for key, value in OPTIONAL_BLANK.items():
        monkeypatch.setenv(key, value)
    settings = load()

    assert settings.download_remove_immediately is False
    assert settings.sync_interval_minutes == 30
    assert settings.watch_interval_seconds == 30
    assert settings.download_quiet_seconds == 120
    assert settings.import_mode == "copy"
    assert settings.mark_reading_after_minutes == 1.0
    assert settings.mark_read_tail_minutes == 30.0
    # Paths too: Path("") is the process CWD, which would put the database
    # somewhere other than the mounted /config.
    assert settings.config_dir == Path("/config")
    assert settings.download_dir == Path("/downloads")
    assert settings.library_dir == Path("/audiobooks")
    assert settings.imports_dir == Path("/imports")


def test_blank_keeps_meaning_where_the_default_is_empty(clean_env, monkeypatch):
    """Empty is a real value for these — it disables the feature."""
    for key, value in OPTIONAL_BLANK.items():
        monkeypatch.setenv(key, value)
    settings = load()

    assert settings.download_client == ""
    assert settings.download_url == ""
    assert settings.download_label == ""
    assert settings.index_url == ""
    assert settings.downloads_enabled is False


def test_values_still_win(clean_env, monkeypatch):
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("DOWNLOAD_REMOVE_IMMEDIATELY", "true")
    monkeypatch.setenv("IMPORT_MODE", "move")
    monkeypatch.setenv("MARK_READ_TAIL_MINUTES", "0")
    monkeypatch.setenv("LIBRARY_DIR", "/srv/books")
    settings = load()

    assert settings.sync_interval_minutes == 5
    assert settings.download_remove_immediately is True
    assert settings.import_mode == "move"
    assert settings.mark_read_tail_minutes == 0.0
    assert settings.library_dir == Path("/srv/books")


def test_blank_admin_password_stays_blank(clean_env, monkeypatch):
    """app/main.py refuses to start on this — it must not become a default."""
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    assert load().admin_password == ""
