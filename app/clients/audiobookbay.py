"""AudioBookBay indexer: HTML scraping, there is no API.

Contract notes (verified against the live site and the prowlarr-abb fork):
- ABB blocks non-browser User-Agents, so every request sends a Chrome UA.
- Search is ``GET /?s={term}&tt=1`` with the term lowercased and non-word
  characters collapsed to spaces; page N is ``/page/{N}/``. An exhausted page
  returns HTTP 200 with zero posts (not a 404).
- Result metadata (Format/Bitrate/File Size) lives in text lines separated by
  ``<br>`` with values in inline ``<span>``s — parse the *text* after
  replacing ``<br>`` with newlines, not raw HTML.
- Some mirrors serve posts (``div.post.re-ab``) whose content is
  base64-encoded HTML; decode before parsing.
- A release's details page carries the torrent's info hash
  (``<td>Info Hash:</td><td>{hex}</td>``) and its tracker list; ``grab``
  builds a public magnet link from those. Mirrors rotate domains and some run
  with expired TLS certs, so INDEX_URL may deliberately be plain http.
"""

import base64
import logging
import re
from datetime import date, datetime
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from app.clients.indexer import GrabResult, IndexerError, IndexerRelease

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_PAGES = 2
# Used only when a details page lists no trackers of its own.
FALLBACK_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
)

SIZE_RE = re.compile(r"File Size:\s*(.+?)s?$", re.MULTILINE)
POSTED_RE = re.compile(r"Posted:\s*(\d{1,2} \D{3} \d{4})")
FORMAT_RE = re.compile(r"Format:\s*([A-Za-z0-9]+)")
INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _text_with_newlines(el: Tag) -> str:
    """Element text with <br> treated as line breaks (values sit in inline
    spans, so a plain separator would split lines mid-field)."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    return el.get_text()


def _parse_size(text: str) -> int | None:
    match = re.match(r"([\d.,]+)\s*([A-Za-z]+)", text.strip())
    if not match:
        return None
    unit = match.group(2).upper().rstrip("S")
    if unit not in SIZE_UNITS:
        return None
    try:
        return int(float(match.group(1).replace(",", "")) * SIZE_UNITS[unit])
    except ValueError:
        return None


class AudioBookBayClient:
    name = "AudioBookBay"

    def __init__(self, index_url: str, timeout: float = 30.0):
        self._base_url = index_url.rstrip("/")
        self._client = httpx.Client(
            headers={"User-Agent": BROWSER_UA},
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AudioBookBayClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def search(self, query: str) -> list[IndexerRelease]:
        term = re.sub(r"[\W_]+", " ", query.lower()).strip()
        releases: list[IndexerRelease] = []
        for page in range(1, MAX_PAGES + 1):
            path = "/" if page == 1 else f"/page/{page}/"
            response = self._client.get(
                f"{self._base_url}{path}", params={"s": term, "tt": "1"}
            )
            if response.status_code == 404:  # past the last page on some mirrors
                break
            response.raise_for_status()
            page_releases = self._parse_search_page(response.text)
            releases.extend(page_releases)
            if not page_releases:
                break
        return releases

    def grab(self, guid: str) -> GrabResult:
        response = self._client.get(guid)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        title_el = soup.select_one("div.postTitle h1") or soup.select_one("div.postTitle")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            raise IndexerError(f"no title found on details page {guid}")

        info_hash = self._find_cell_after(soup, "Info Hash:")
        if not info_hash or not INFO_HASH_RE.match(info_hash):
            raise IndexerError(f"no valid info hash found on details page {guid}")
        info_hash = info_hash.lower()

        trackers = self._find_trackers(soup) or list(FALLBACK_TRACKERS)
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}" + "".join(
            f"&tr={quote(t, safe='')}" for t in trackers
        )
        return GrabResult(info_hash=info_hash, magnet_uri=magnet, title=title)

    def check(self) -> str:
        response = self._client.get(f"{self._base_url}/")
        response.raise_for_status()
        return "reachable"

    # -- parsing helpers ---------------------------------------------------

    def _parse_search_page(self, html: str) -> list[IndexerRelease]:
        soup = BeautifulSoup(html, "html.parser")
        releases = []
        for post in soup.select("div.post"):
            if "re-ab" in post.get("class", []):
                post = self._decode_reab(post)
                if post is None:
                    continue
            release = self._parse_post(post)
            if release is not None:
                releases.append(release)
        return releases

    @staticmethod
    def _decode_reab(post: Tag) -> Tag | None:
        """Some mirrors base64-encode post bodies (class ``re-ab``)."""
        try:
            decoded = base64.b64decode(post.get_text(strip=True)).decode("utf-8")
            return BeautifulSoup(decoded, "html.parser")
        except Exception:
            logger.warning("could not decode base64 post, skipping")
            return None

    def _parse_post(self, post: Tag) -> IndexerRelease | None:
        link = post.select_one("div.postTitle h2 a")
        if link is None or not link.get("href"):
            return None
        title = link.get_text(strip=True)
        guid = urljoin(f"{self._base_url}/", link["href"])

        content = post.select_one("div.postContent") or post
        text = _text_with_newlines(content)

        size = None
        if size_match := SIZE_RE.search(text):
            size = _parse_size(size_match.group(1))
        published = None
        if posted_match := POSTED_RE.search(text):
            try:
                published = datetime.strptime(posted_match.group(1), "%d %b %Y").date()
            except ValueError:
                pass
        format_ = None
        if format_match := FORMAT_RE.search(text):
            format_ = format_match.group(1).upper()

        cover = content.select_one("img[src]")
        cover_url = urljoin(f"{self._base_url}/", cover["src"]) if cover else None

        return IndexerRelease(
            guid=guid,
            indexer=self.name,
            title=title,
            size=size,
            published=published,
            format=format_,
            cover_url=cover_url,
        )

    @staticmethod
    def _find_cell_after(soup: BeautifulSoup, label: str) -> str | None:
        for td in soup.find_all("td"):
            if td.get_text(strip=True) == label:
                sibling = td.find_next_sibling("td")
                if sibling is not None:
                    return sibling.get_text(strip=True)
        return None

    @staticmethod
    def _find_trackers(soup: BeautifulSoup) -> list[str]:
        trackers: list[str] = []
        for td in soup.find_all("td"):
            if td.get_text(strip=True) in ("Announce URL:", "Tracker:"):
                sibling = td.find_next_sibling("td")
                url = sibling.get_text(strip=True) if sibling else ""
                if url.startswith(("http://", "https://", "udp://")) and url not in trackers:
                    trackers.append(url)
        return trackers
