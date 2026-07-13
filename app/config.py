from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hardcover_token: str = ""
    prowlarr_url: str = "http://host.docker.internal:9696"
    prowlarr_api_key: str = ""
    download_dir: Path = Path("/downloads")
    library_dir: Path = Path("/audiobooks")
    config_dir: Path = Path("/config")
    sync_interval_minutes: int = 30

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.config_dir / 'audiobooklibrary.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
