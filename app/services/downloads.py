"""Prowlarr search and grab: find audiobook releases and track what we asked
Prowlarr to download."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.clients.prowlarr import ProwlarrClient
from app.config import get_settings
from app.models import Book, DownloadState, Release

logger = logging.getLogger(__name__)


def _client() -> ProwlarrClient:
    settings = get_settings()
    return ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)


def search_releases(book: Book) -> list[dict[str, Any]]:
    """Search Prowlarr for a book: '{author} {title}', falling back to just
    the title. Results sorted by seeders, best first."""
    categories = get_settings().category_ids
    with _client() as client:
        results = client.search(f"{book.author.name} {book.title}", categories)
        if not results:
            results = client.search(book.title, categories)
    results.sort(key=lambda r: r.get("seeders") or 0, reverse=True)
    return results


def grab_release(
    session: Session,
    book: Book,
    guid: str,
    indexer_id: int,
    title: str,
    size: int | None,
    seeders: int | None,
) -> Release:
    """Ask Prowlarr to grab a release and record it for download matching."""
    with _client() as client:
        client.grab(guid, indexer_id)

    release = Release(
        book=book,
        prowlarr_guid=guid,
        indexer_id=indexer_id,
        title=title,
        size=size,
        seeders=seeders,
        grabbed_at=datetime.now(timezone.utc),
        status="grabbed",
    )
    book.download_state = DownloadState.GRABBED
    session.add(release)
    session.commit()
    logger.info("Grabbed release for %s: %s", book.title, title)
    return release
