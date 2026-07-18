"""Search a torrent indexer for audiobook releases and hand the chosen one to
the download client.

Grabbing is two steps: the indexer resolves the release's details page to a
magnet link, and the download client (Deluge) adds it. We record the torrent's
info hash so the watcher can ask the client how the download is going instead
of guessing from the download directory — and so cancelling can take the
torrent back out again.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.download_client import get_download_client
from app.clients.indexer import IndexerRelease, get_indexer
from app.config import get_settings
from app.models import Book, DownloadState, Edition, Release, User
from app.services.editions import get_or_create_edition

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


def drop_from_client(release: Release) -> None:
    """Remove a release's torrent and its downloaded data from the client.

    Called when the user cancels: they want the download gone, so it goes —
    including any seeding of it. Raises if the client can't be reached, so the
    caller can tell the user the torrent may still be running.
    """
    if not release.info_hash or not get_settings().download_url:
        return
    with get_download_client() as client:
        client.remove_torrent(release.info_hash, remove_data=True)
    logger.info("Removed torrent %s (with data) for %s", release.info_hash, release.title)


def grab_release(
    session: Session,
    user: User | None,
    book: Book,
    guid: str,
    indexer_name: str,
    title: str,
    size: int | None,
    edition: Edition | None = None,
) -> Release:
    """Resolve a release to a magnet and add it to the download client. The
    download lands on `edition` (the book's unlabelled edition by default)."""
    with get_indexer() as indexer:
        grabbed = indexer.grab(guid)
    with get_download_client() as client:
        info_hash = client.add_magnet(grabbed.magnet_uri)

    if edition is None:
        edition = get_or_create_edition(session, book)
    release = Release(
        edition=edition,
        user=user,
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
    edition.download_state = DownloadState.GRABBED
    session.add(release)
    session.commit()
    logger.info("Grabbed release for %s: %s (%s)", book.title, release.title, release.info_hash)
    return release
