from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Password for the virtual "admin" account (user administration only).
    # Required: the app refuses to start without it.
    admin_password: str = ""

    # Torrent indexer (AudioBookBay) and the torrent client that downloads it.
    index_url: str = ""
    download_client: str = "deluge"
    download_url: str = ""
    # Deluge's web UI authenticates on the password alone; the username is
    # accepted and unused, reserved for clients that need one.
    download_username: str = ""
    download_password: str = ""

    download_dir: Path = Path("/downloads")
    library_dir: Path = Path("/audiobooks")
    config_dir: Path = Path("/config")
    # Hard-coded volume path (undocumented setting; overridden only by tests).
    imports_dir: Path = Path("/imports")
    sync_interval_minutes: int = 30
    watch_interval_seconds: int = 30
    # A download is considered finished when nothing in it changed for this long.
    download_quiet_seconds: int = 120
    # "copy" hardlinks (falls back to copying) so seeding torrents keep their
    # files; "move" relocates them out of the download directory.
    import_mode: str = "copy"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.config_dir / 'audiobooklibrary.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
