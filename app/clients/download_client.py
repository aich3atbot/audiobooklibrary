"""Download-client abstraction.

The app hands a magnet link (from an ``Indexer``) to a torrent client and then
polls it by info hash to learn when the download is finished. Today the only
implementation is Deluge (``app.clients.deluge``); others plug in by
implementing the ``DownloadClient`` protocol and extending
``get_download_client``.

``remove_torrent`` deletes the torrent *and its data*, which also ends any
seeding of it. That is the intended behaviour of cancelling a release: the
user asked for the download to go away, so it goes away.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class DownloadClientError(RuntimeError):
    code: int | None = None  # client-specific error code, when the client reports one


@dataclass
class TorrentStatus:
    info_hash: str
    name: str  # the torrent's real name, which may differ from the release title
    progress: float  # 0.0–100.0
    state: str  # "Downloading", "Seeding", "Queued", "Paused", "Error", ...
    is_finished: bool  # the completion signal — a finished torrent may sit in any state
    save_path: str  # in the *client's* filesystem namespace, not ours
    total_size: int


@runtime_checkable
class DownloadClient(Protocol):
    def add_magnet(self, magnet_uri: str) -> str:
        """Add a magnet; returns its info hash."""
        ...

    def get_status(self, info_hashes: Sequence[str]) -> dict[str, TorrentStatus]:
        """Status for the given hashes, keyed by lowercase hash. Hashes the
        client doesn't know are simply absent."""
        ...

    def remove_torrent(self, info_hash: str, remove_data: bool = True) -> None:
        """Remove a torrent, by default deleting its downloaded data too. A
        torrent the client no longer has is not an error — the goal state is
        "gone", and it is already there."""
        ...

    def check(self) -> str:
        """Human-readable connection status; raises on failure."""
        ...

    def close(self) -> None: ...


def get_download_client(timeout: float = 30.0) -> DownloadClient:
    """Build the configured download client from settings."""
    from app.clients.deluge import DelugeClient
    from app.config import get_settings

    settings = get_settings()
    if settings.download_client != "deluge":
        raise DownloadClientError(
            f"unsupported DOWNLOAD_CLIENT: {settings.download_client!r} (only 'deluge')"
        )
    if not settings.download_url:
        raise DownloadClientError("DOWNLOAD_URL is not set")
    return DelugeClient(settings.download_url, settings.download_password, timeout=timeout)
