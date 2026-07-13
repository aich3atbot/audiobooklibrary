from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Single-user login; auth is enforced only when both are set.
    auth_username: str = ""
    auth_password: str = ""

    hardcover_token: str = ""
    prowlarr_url: str = "http://host.docker.internal:9696"
    prowlarr_api_key: str = ""
    prowlarr_categories: str = "3030"  # comma-separated torznab category ids

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
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password)

    @property
    def category_ids(self) -> tuple[int, ...]:
        return tuple(int(c) for c in self.prowlarr_categories.split(",") if c.strip())

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.config_dir / 'audiobooklibrary.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
