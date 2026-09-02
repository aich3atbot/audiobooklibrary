import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Password for the "admin" account (user administration only). Required
    # on a fresh database, where it creates the account; afterwards it is
    # optional, and setting it changes the stored password (see
    # app.services.users.ensure_admin_account).
    admin_password: str = ""

    # Torrent indexer (AudioBookBay) and the torrent client that downloads it.
    # Downloading is enabled only when both DOWNLOAD_CLIENT and DOWNLOAD_URL
    # are set; leaving either empty disables it entirely (the download UI is
    # hidden; manual import and the rest keep working).
    index_url: str = ""
    download_client: str = ""
    download_url: str = ""
    # qBittorrent needs both; Deluge's web UI authenticates on the password
    # alone and ignores the username.
    download_username: str = ""
    download_password: str = ""
    # Label to tag the app's torrents with in the client (Deluge Label plugin /
    # qBittorrent category). Empty (the default) disables labeling.
    download_label: str = ""
    # Remove the torrent (and its data) from the client right after a
    # successful import. Off by default: the torrent keeps seeding per the
    # client's own settings.
    download_remove_immediately: bool = False

    # The image's VOLUME mount points, and *not* part of a deployment's
    # configuration surface: docker-compose.yml never passes CONFIG_DIR /
    # DOWNLOAD_DIR / LIBRARY_DIR into the container — there those names are
    # compose's own variables, naming the *host* side of each bind mount, which
    # is the opposite of what the fields below mean. A deployment therefore
    # picks its paths with `volumes:`, and README documents no path variables.
    # pydantic-settings still binds each field to its uppercased name, which is
    # how a dev checkout's .env and the tests point them elsewhere; setting one
    # inside a container would only desync the app from the volumes it declares.
    download_dir: Path = Path("/downloads")
    library_dir: Path = Path("/audiobooks")
    config_dir: Path = Path("/config")
    # Same, but deliberately undocumented even for a dev run: /imports is the
    # collection-import staging area everywhere, overridden only by tests.
    imports_dir: Path = Path("/imports")
    sync_interval_minutes: int = 30
    download_watch_interval_seconds: int = 30
    # A download is considered finished when nothing in it changed for this long.
    download_quiet_seconds: int = 120
    # How a *finished download* becomes library files: "copy" hardlinks (falls
    # back to copying) so seeding torrents keep their files, "move" relocates
    # them out of the download directory. The DOWNLOAD_ prefix is load-bearing —
    # importing from /imports always moves and never consults this.
    download_import_mode: str = "copy"

    # Converting an MP3 edition to a single chaptered m4b (see
    # app/services/transcode.py). The target AAC bitrate is halved for a book
    # that is mono throughout and is never raised above the source's own
    # bitrate. ffprobe is deliberately not used — mutagen already reports MP3
    # durations — so only ffmpeg has to exist.
    transcode_bitrate: str = "64k"
    ffmpeg_path: str = "ffmpeg"

    # How listening moves a book's read state (see app/abs/progress.py).
    # Listened this long and the book counts as started; 0 marks it "currently
    # reading" on the first progress sync after playback begins.
    mark_reading_after_minutes: float = 1.0
    # How much trailing credits to forgive: a book left this close to the end
    # is marked read once another book starts. 0 requires a complete listen.
    mark_read_tail_minutes: float = 30.0

    @model_validator(mode="before")
    @classmethod
    def _blank_means_default(cls, data: Any) -> Any:
        """An empty environment value falls back to the field's default.

        Optional variables are passed through bare by docker-compose, so an
        unset one never reaches us at all — but a variable set to "" in .env
        still arrives as an empty string, and parsing that as an int or a bool
        would abort startup. Fields whose default is "" keep it: empty is
        meaningful there (no DOWNLOAD_LABEL means no label, no DOWNLOAD_CLIENT
        disables downloading).
        """
        if not isinstance(data, dict):
            return data
        fields = cls.model_fields
        return {
            key: value
            for key, value in data.items()
            if value != ""
            or key not in fields
            or fields[key].get_default(call_default_factory=True) == ""
        }

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.config_dir / 'audiobooklibrary.db'}"

    @property
    def downloads_enabled(self) -> bool:
        return bool(self.download_client and self.download_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_session_secret() -> str:
    """Generate the cookie-signing secret once and persist it in the config
    dir so sessions survive restarts. Not configurable by design.

    Lives here rather than in `app/auth.py` because the ABS tokens are signed
    with it too (`app/abs/tokens.py`), and auth reaches into `app/abs` for the
    session store — one of the two directions had to give."""
    settings = get_settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    path = settings.config_dir / "session_secret"
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_hex(32)
    path.write_text(secret)
    path.chmod(0o600)
    return secret
