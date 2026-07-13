from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.clients.audiobookbay import AudioBookBayClient
from app.clients.indexer import IndexerError

ABB = "http://abb.test"
FIXTURES = Path(__file__).parent / "fixtures" / "abb"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def abb():
    with AudioBookBayClient(ABB) as client:
        yield client


@respx.mock
def test_search_parses_releases(abb):
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(200, text=fixture("search.html")))
    respx.get(f"{ABB}/page/2/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )

    releases = abb.search("Andy Weir Project Hail Mary")

    assert len(releases) == 2
    first = releases[0]
    assert first.title == "Andy Weir - Project Hail Mary"
    assert first.guid == f"{ABB}/abss/prokject-hail-mary-andy-weir/"  # relative -> absolute
    assert first.size == int(881.82 * 1024**2)
    assert first.published == date(2021, 11, 14)
    assert first.format == "M4B"
    assert first.cover_url == "https://m.media-amazon.com/images/I/51b6fvQr1-L.jpg"
    assert first.indexer == "AudioBookBay"

    second = releases[1]
    assert second.format == "MP3"
    assert second.size == int(1.2 * 1024**3)
    assert second.cover_url == f"{ABB}/images/covers/phm.jpg"


@respx.mock
def test_search_normalizes_query_and_sends_browser_ua(abb):
    route = respx.get(f"{ABB}/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )

    abb.search("Andy Weir: Project_Hail Mary!")

    request = route.calls[0].request
    assert request.url.params["s"] == "andy weir project hail mary"
    assert request.url.params["tt"] == "1"
    assert "python-httpx" not in request.headers["user-agent"].lower()
    assert "Mozilla/5.0" in request.headers["user-agent"]


@respx.mock
def test_search_stops_paging_when_a_page_has_no_posts(abb):
    page1 = respx.get(f"{ABB}/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )
    page2 = respx.get(f"{ABB}/page/2/").mock(
        return_value=httpx.Response(200, text=fixture("search.html"))
    )

    assert abb.search("nothing here") == []
    assert page1.called
    assert not page2.called  # exhausted pages return 200, so stop on zero posts


@respx.mock
def test_search_decodes_base64_reab_posts(abb):
    respx.get(f"{ABB}/").mock(
        return_value=httpx.Response(200, text=fixture("search_reab.html"))
    )
    respx.get(f"{ABB}/page/2/").mock(
        return_value=httpx.Response(200, text=fixture("search_empty.html"))
    )

    releases = abb.search("secret book")

    assert len(releases) == 1
    assert releases[0].title == "Hidden Author - Secret Book"
    assert releases[0].guid == f"{ABB}/abss/secret-book-hidden/"
    assert releases[0].size == int(512.06 * 1024**2)


@respx.mock
def test_grab_builds_magnet_from_details_page(abb):
    guid = f"{ABB}/abss/prokject-hail-mary-andy-weir/"
    respx.get(guid).mock(return_value=httpx.Response(200, text=fixture("details.html")))

    result = abb.grab(guid)

    assert result.info_hash == "ad5fae5ffda056f9f45131045d140326bbafc4dc"
    assert result.title == "Project Hail Mary - Andy Weir"
    assert result.magnet_uri.startswith(f"magnet:?xt=urn:btih:{result.info_hash}")
    assert "dn=Project%20Hail%20Mary%20-%20Andy%20Weir" in result.magnet_uri
    # trackers scraped from the details page, deduplicated
    assert result.magnet_uri.count("&tr=") == 3
    assert "udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce" in result.magnet_uri


@respx.mock
def test_grab_without_info_hash_raises(abb):
    guid = f"{ABB}/abss/broken/"
    respx.get(guid).mock(
        return_value=httpx.Response(
            200,
            text="<div class='postTitle'><h1>Broken</h1></div><table><tr>"
            "<td>Info Hash:</td><td>not-a-hash</td></tr></table>",
        )
    )

    with pytest.raises(IndexerError, match="info hash"):
        abb.grab(guid)


@respx.mock
def test_grab_falls_back_to_public_trackers(abb):
    guid = f"{ABB}/abss/no-trackers/"
    respx.get(guid).mock(
        return_value=httpx.Response(
            200,
            text="<div class='postTitle'><h1>Lonely Book</h1></div><table><tr>"
            "<td>Info Hash:</td><td>AD5FAE5FFDA056F9F45131045D140326BBAFC4DC</td>"
            "</tr></table>",
        )
    )

    result = abb.grab(guid)

    assert result.info_hash == "ad5fae5ffda056f9f45131045d140326bbafc4dc"  # lowercased
    assert result.magnet_uri.count("&tr=") == 5


@respx.mock
def test_check_hits_the_index_root(abb):
    route = respx.get(f"{ABB}/").mock(return_value=httpx.Response(200, text="<html></html>"))

    assert abb.check() == "reachable"
    assert route.called
