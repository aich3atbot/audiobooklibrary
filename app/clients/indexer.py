"""Torrent indexer abstraction.

An Indexer searches a torrent site for audiobook releases and turns a chosen
release into a magnet link (via ``grab``). The app hands the magnet to a
download client (see ``app.clients.download_client``); the indexer never
downloads anything itself. Today the only implementation is AudioBookBay
(``app.clients.audiobookbay``); new sites plug in by implementing the
``Indexer`` protocol and extending ``get_indexer``.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


class IndexerError(RuntimeError):
    pass


@dataclass
class IndexerRelease:
    """One search result. ``guid`` is the release's details-page URL — unique
    per indexer and sufficient to grab later."""

    guid: str
    indexer: str
    title: str
    size: int | None = None  # bytes
    published: date | None = None
    format: str | None = None  # e.g. "M4B", "MP3"
    cover_url: str | None = None


@dataclass
class GrabResult:
    """What ``grab`` resolves a release to: a magnet the download client can add."""

    info_hash: str  # 40-char lowercase hex
    magnet_uri: str
    title: str  # display title from the details page (the magnet's dn)


@runtime_checkable
class Indexer(Protocol):
    name: str

    def search(self, query: str) -> list[IndexerRelease]: ...

    def grab(self, guid: str) -> GrabResult: ...

    def check(self) -> str:
        """Human-readable connection status; raises on failure."""
        ...

    def close(self) -> None: ...


def get_indexer() -> Indexer:
    """Build the configured indexer from settings (always AudioBookBay for now)."""
    from app.clients.audiobookbay import AudioBookBayClient
    from app.config import get_settings

    settings = get_settings()
    if not settings.index_url:
        raise IndexerError("INDEX_URL is not set")
    return AudioBookBayClient(settings.index_url)
