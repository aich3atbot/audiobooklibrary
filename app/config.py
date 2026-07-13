from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Single-user login; auth is enforced only when both are set.
    auth_username: str = ""
    auth_password: str = ""
    # Signs the session cookie. Optional: generated and persisted in
    # config_dir when unset.
    session_secret: str = ""

    hardcover_token: str = ""
    prowlarr_url: str = "http://host.docker.internal:9696"
    prowlarr_api_key: str = ""
    prowlarr_categories: str = "3030"  # comma-separated torznab category ids
    download_dir: Path = Path("/downloads")
    library_dir: Path = Path("/audiobooks")
    config_dir: Path = Path("/config")
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
