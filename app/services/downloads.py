"""Search a torrent indexer for audiobook releases and hand the chosen one to
the download client.

Grabbing is two steps: the indexer resolves the release's details page to a
magnet link, and the download client (Deluge) adds it. We record the torrent's
info hash so the watcher can ask the client how the download is going instead
of guessing from the download directory — and so cancelling can take the
torrent back out again.
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.audiobookbay import search_term
from app.clients.download_client import get_download_client
from app.clients.hardcover import HardcoverClient
from app.clients.indexer import IndexerRateLimited, IndexerRelease, get_indexer
from app.config import get_settings
from app.models import Book, DownloadState, Edition, Release, User
from app.services.editions import get_or_create_edition

logger = logging.getLogger(__name__)

# The audio-title lookup is an optional extra query, so it gets a short leash.
AUDIO_TITLE_TIMEOUT = 10.0
# A title cut at its colon is only searched without the author when it still
# says something: "Four" (from "Four: The Transfer") matched 18 unrelated posts.
MIN_BARE_MAIN_TITLE_WORDS = 2


@dataclass
class ReleaseSearch:
    releases: list[IndexerRelease] = field(default_factory=list)
    query: str = ""  # the last query sent — what produced `releases`
    error: str | None = None


def main_title(title: str) -> str:
    """The title before its first colon — drops a subtitle like ": A LitRPG
    Adventure" that the release name usually doesn't carry."""
    return title.split(":", 1)[0].strip()


def candidate_queries(book: Book, audio_title: Callable[[], str | None]) -> Iterator[str]:
    """Queries for a book, most specific first; the caller stops at the first
    that finds anything. `audio_title` is only called if the first query came
    up empty (it costs a Hardcover request).

    Order matters (measured against the live indexer, see plan.md): the full
    title first, so a book that works today never gets a broader search; the
    audiobook edition's title next, as the most specific other source; the
    colon cut last, as it broadens."""
    author = book.author.name if book.author else ""
    title = book.title
    short = main_title(title)

    def with_author(text: str) -> str:
        return f"{author} {text}".strip()

    yield with_author(title)
    if audio := audio_title():
        yield with_author(audio)
    if short:
        yield with_author(short)
    yield title
    if short and len(search_term(short).split()) >= MIN_BARE_MAIN_TITLE_WORDS:
        yield short


def _audio_title_lookup(book: Book, user: User | None) -> Callable[[], str | None]:
    def lookup() -> str | None:
        if user is None or not user.hardcover_token:
            return None
        try:
            with HardcoverClient(user.hardcover_token, timeout=AUDIO_TITLE_TIMEOUT) as client:
                return client.fetch_audio_title(book.hardcover_id)
        except Exception:
            # Optional: a missing or limited lookup just skips this query.
            logger.warning("Audio title lookup failed for %s", book.title, exc_info=True)
            return None

    return lookup


def search_releases(book: Book, user: User | None = None, query: str | None = None) -> ReleaseSearch:
    """Search the indexer for a book, trying `candidate_queries` in order and
    stopping at the first that finds anything — or just `query` when the user
    typed one. Results keep the indexer's own relevance order — AudioBookBay
    publishes no seeder counts, so there is nothing better to rank on.

    A rate limit stops the chain at once (every further query would be limited
    too) and is reported as the result's `error`; other failures raise."""
    queries = [query.strip()] if query and query.strip() else candidate_queries(
        book, _audio_title_lookup(book, user)
    )
    result = ReleaseSearch()
    tried: set[str] = set()
    with get_indexer() as indexer:
        for q in queries:
            term = search_term(q)
            if not term or term in tried:
                continue
            tried.add(term)
            result.query = q
            try:
                result.releases = indexer.search(q)
            except IndexerRateLimited as exc:
                result.error = (
                    f"{exc.service} is limiting requests — try again in {exc.wait_seconds}s."
                )
                return result
            if result.releases:
                break
    return result


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
