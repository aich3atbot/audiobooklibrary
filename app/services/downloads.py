"""Search a torrent indexer for audiobook releases and hand the chosen one to
the download client.

Grabbing is two steps: the indexer resolves the release's details page to a
magnet link, and the download client (Deluge) adds it. We record the torrent's
info hash so the watcher can ask the client how the download is going instead
of guessing from the download directory.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.download_client import get_download_client
from app.clients.indexer import IndexerRelease, get_indexer
from app.models import Book, DownloadState, Release

logger = logging.getLogger(__name__)


def search_releases(book: Book) -> list[IndexerRelease]:
    """Search the indexer for a book: '{author} {title}', falling back to just
    the title. Results keep the indexer's own relevance order — AudioBookBay
    publishes no seeder counts, so there is nothing better to rank on."""
    with get_indexer() as indexer:
        results = indexer.search(f"{book.author.name} {book.title}")
        if not results:
            results = indexer.search(book.title)
    return results


def grab_release(
    session: Session,
    book: Book,
    guid: str,
    indexer_name: str,
    title: str,
    size: int | None,
) -> Release:
    """Resolve a release to a magnet and add it to the download client."""
    with get_indexer() as indexer:
        grabbed = indexer.grab(guid)
    with get_download_client() as client:
        info_hash = client.add_magnet(grabbed.magnet_uri)

    release = Release(
        book=book,
        guid=guid,
        indexer=indexer_name,
        # The details-page title is what the magnet names the download, so it
        # is the better key if we ever have to match by folder name.
        title=grabbed.title or title,
        size=size,
        info_hash=(info_hash or grabbed.info_hash).lower(),
        magnet_uri=grabbed.magnet_uri,
        progress=0.0,
        grabbed_at=datetime.now(timezone.utc),
        status="grabbed",
    )
    book.download_state = DownloadState.GRABBED
    session.add(release)
    session.commit()
    logger.info("Grabbed release for %s: %s (%s)", book.title, release.title, release.info_hash)
    return release
