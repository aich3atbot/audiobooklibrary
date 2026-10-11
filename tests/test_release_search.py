"""Release search query order and rate-limit backoff (indexer and Hardcover)."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.clients import ratelimit
from app.clients.audiobookbay import AudioBookBayClient
from app.clients.hardcover import API_URL, HardcoverClient, HardcoverError, HardcoverRateLimited
from app.clients.indexer import IndexerRateLimited
from app.models import AppState, Author, Book, DownloadState, Edition, Release, Series, User, UserBook
from app.services.downloads import candidate_queries, main_title, search_releases
from app.services.sync import run_sync_for_user

ABB = "http://abb.test"
FIXTURES = Path(__file__).parent / "fixtures" / "abb"
HITS = (FIXTURES / "search.html").read_text()
EMPTY = (FIXTURES / "search_empty.html").read_text()


@pytest.fixture(autouse=True)
def indexer_config(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "index_url", ABB)


@pytest.fixture
def clean_db(db_session):
    for model in (UserBook, Release, Edition, Book, Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


def make_book(db, title: str, author: str, hardcover_id: int = 2707026) -> Book:
    book = Book(hardcover_id=hardcover_id, title=title, author=Author(hardcover_id=hardcover_id, name=author))
    db.add(book)
    db.commit()
    return book


def mock_abb(hit_terms: set[str] = frozenset()):
    """ABB answering hits for the given search terms and nothing otherwise.
    Returns the page-1 route; its calls' `s` params are the terms searched."""
    def page1(request: httpx.Request) -> httpx.Response:
        term = request.url.params["s"]
        return httpx.Response(200, text=HITS if term in hit_terms else EMPTY)

    route = respx.get(f"{ABB}/").mock(side_effect=page1)
    respx.get(f"{ABB}/page/2/").mock(return_value=httpx.Response(200, text=EMPTY))
    return route


def mock_audio_title(title: str | None):
    edition = {"title": title} if title is not None else None
    return respx.post(API_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"books": [{"default_audio_edition": edition}]}}
        )
    )


def searched(route) -> list[str]:
    return [call.request.url.params["s"] for call in route.calls]


# -- query order -----------------------------------------------------------


def test_main_title_cuts_at_the_first_colon():
    assert main_title("He Who Fights with Monsters 13: A LitRPG Adventure") == (
        "He Who Fights with Monsters 13"
    )
    assert main_title("Four: The Transfer: A Divergent Story") == "Four"
    assert main_title("Coraline") == "Coraline"


def test_audio_title_is_only_looked_up_once_the_first_query_fails(clean_db):
    book = make_book(clean_db, "Four: The Transfer", "Veronica Roth")
    lookups = []

    queries = candidate_queries(book, lambda: lookups.append(1) or None)

    assert next(queries) == "Veronica Roth Four: The Transfer"
    assert lookups == []
    assert next(queries) == "Veronica Roth Four"
    assert lookups == [1]


@respx.mock
def test_subtitled_title_is_found_by_its_audiobook_edition_title(clean_db, user):
    book = make_book(clean_db, "He Who Fights with Monsters 13: A LitRPG Adventure", "Shirtaloon")
    abb = mock_abb({"shirtaloon he who fights with monsters 13"})
    hardcover = mock_audio_title("He Who Fights with Monsters 13")

    found = search_releases(book, user)

    assert searched(abb) == [
        "shirtaloon he who fights with monsters 13 a litrpg adventure",
        "shirtaloon he who fights with monsters 13",
    ]
    assert hardcover.call_count == 1
    assert len(found.releases) == 2
    assert found.query == "Shirtaloon He Who Fights with Monsters 13"
    assert found.error is None


@respx.mock
def test_colon_cut_follows_an_audio_title_that_misses(clean_db, user):
    book = make_book(clean_db, "Four: A Divergent Collection", "Veronica Roth")
    abb = mock_abb({"veronica roth four"})
    mock_audio_title("Four: A Divergent Story Collection")

    found = search_releases(book, user)

    assert searched(abb) == [
        "veronica roth four a divergent collection",
        "veronica roth four a divergent story collection",
        "veronica roth four",
    ]
    assert found.query == "Veronica Roth Four"


@respx.mock
def test_a_working_full_title_never_asks_hardcover(clean_db, user):
    book = make_book(clean_db, "Four: The Transfer", "Veronica Roth")
    abb = mock_abb({"veronica roth four the transfer"})
    hardcover = mock_audio_title("Four: The Transfer: A Divergent Story")

    found = search_releases(book, user)

    assert searched(abb) == ["veronica roth four the transfer"]
    assert hardcover.call_count == 0
    assert len(found.releases) == 2


@respx.mock
def test_one_word_main_title_is_never_searched_alone(clean_db, user):
    book = make_book(clean_db, "Four: The Transfer", "Veronica Roth")
    abb = mock_abb()
    mock_audio_title(None)

    found = search_releases(book, user)

    assert searched(abb) == [
        "veronica roth four the transfer",
        "veronica roth four",
        "four the transfer",
    ]
    assert found.releases == []
    assert found.query == "Four: The Transfer"


@respx.mock
def test_multi_word_main_title_is_the_last_resort(clean_db, user):
    book = make_book(clean_db, "He Who Fights with Monsters 13: A LitRPG Adventure", "Shirtaloon")
    abb = mock_abb()
    mock_audio_title(None)

    search_releases(book, user)

    assert searched(abb)[-1] == "he who fights with monsters 13"


@respx.mock
def test_queries_that_normalise_alike_are_sent_once(clean_db, user):
    # the audio title differs only by punctuation, and there is no colon
    book = make_book(clean_db, "Harry Potter and the Philosopher's Stone", "J.K. Rowling")
    abb = mock_abb()
    mock_audio_title("Harry Potter and the Philosopher’s Stone")

    search_releases(book, user)

    assert searched(abb) == [
        "j k rowling harry potter and the philosopher s stone",
        "harry potter and the philosopher s stone",
    ]


@respx.mock
def test_audio_title_step_is_skipped_without_a_token(clean_db, tokenless_user):
    book = make_book(clean_db, "Four: A Divergent Collection", "Veronica Roth")
    abb = mock_abb({"veronica roth four"})
    hardcover = mock_audio_title("anything")

    found = search_releases(book, tokenless_user)

    assert hardcover.call_count == 0
    assert searched(abb) == ["veronica roth four a divergent collection", "veronica roth four"]
    assert len(found.releases) == 2


@respx.mock
def test_audio_title_step_is_skipped_when_hardcover_fails(clean_db, user):
    book = make_book(clean_db, "Four: A Divergent Collection", "Veronica Roth")
    abb = mock_abb({"veronica roth four"})
    respx.post(API_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    found = search_releases(book, user)

    assert searched(abb) == ["veronica roth four a divergent collection", "veronica roth four"]
    assert found.error is None
    assert len(found.releases) == 2


@respx.mock
def test_typed_query_runs_alone(clean_db, user):
    book = make_book(clean_db, "He Who Fights with Monsters 13: A LitRPG Adventure", "Shirtaloon")
    abb = mock_abb()
    hardcover = mock_audio_title("He Who Fights with Monsters 13")

    found = search_releases(book, user, "  hwfwm 13 ")

    assert searched(abb) == ["hwfwm 13"]
    assert hardcover.call_count == 0
    assert found.query == "hwfwm 13"


# -- indexer rate limits ---------------------------------------------------


@respx.mock
def test_indexer_rate_limit_stops_the_chain_and_the_next_search(clean_db, user):
    book = make_book(clean_db, "He Who Fights with Monsters 13: A LitRPG Adventure", "Shirtaloon")
    abb = respx.get(f"{ABB}/").mock(return_value=httpx.Response(429))
    hardcover = mock_audio_title("He Who Fights with Monsters 13")

    found = search_releases(book, user)

    assert abb.call_count == 1
    assert hardcover.call_count == 0
    assert found.releases == []
    assert found.error == "AudioBookBay is limiting requests — try again in 60s."

    again = search_releases(book, user)

    assert abb.call_count == 1  # nothing sent while backing off
    assert again.error and "limiting requests" in again.error


@respx.mock
def test_retry_after_seconds_sets_the_cooldown():
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))

    with pytest.raises(IndexerRateLimited) as exc:
        AudioBookBayClient(ABB).search("x")

    assert exc.value.wait_seconds == 120
    assert 119 < ratelimit.check("indexer:abb.test") <= 120


@respx.mock
def test_retry_after_http_date_sets_the_cooldown():
    when = datetime.now(timezone.utc) + timedelta(seconds=300)
    respx.get(f"{ABB}/").mock(
        return_value=httpx.Response(503, headers={"Retry-After": format_datetime(when, usegmt=True)})
    )

    with pytest.raises(IndexerRateLimited):
        AudioBookBayClient(ABB).search("x")

    assert 290 < ratelimit.check("indexer:abb.test") <= 300


@respx.mock
def test_503_without_retry_after_is_an_outage_not_a_limit():
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        AudioBookBayClient(ABB).search("x")

    assert ratelimit.check("indexer:abb.test") is None


@respx.mock
def test_403_is_not_a_rate_limit():
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(403))

    with pytest.raises(httpx.HTTPStatusError):
        AudioBookBayClient(ABB).search("x")

    assert ratelimit.check("indexer:abb.test") is None


@respx.mock
def test_limited_second_page_keeps_the_first_pages_results():
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(200, text=HITS))
    respx.get(f"{ABB}/page/2/").mock(return_value=httpx.Response(429))

    releases = AudioBookBayClient(ABB).search("x")

    assert len(releases) == 2
    assert ratelimit.check("indexer:abb.test") is not None


@respx.mock
def test_grab_is_held_back_by_the_cooldown_too():
    ratelimit.trip("indexer:abb.test", 30)
    details = respx.get(f"{ABB}/abss/x/").mock(return_value=httpx.Response(200))

    with pytest.raises(IndexerRateLimited):
        AudioBookBayClient(ABB).grab(f"{ABB}/abss/x/")

    assert details.call_count == 0


@respx.mock
def test_cooldown_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    route = respx.get(f"{ABB}/").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, text=EMPTY)]
    )
    client = AudioBookBayClient(ABB)

    with pytest.raises(IndexerRateLimited):
        client.search("x")
    now[0] += 59
    with pytest.raises(IndexerRateLimited):
        client.search("x")
    assert route.call_count == 1

    now[0] += 2
    assert client.search("x") == []
    assert route.call_count == 2


# -- Hardcover rate limits -------------------------------------------------


@respx.mock
def test_hardcover_429_backs_off_per_token():
    route = respx.post(API_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"data": {"books": []}})]
    )

    with pytest.raises(HardcoverRateLimited) as exc:
        HardcoverClient("token-a").fetch_audio_title(1)
    assert isinstance(exc.value, HardcoverError)  # existing handlers still catch it

    with pytest.raises(HardcoverRateLimited):
        HardcoverClient("token-a").fetch_audio_title(1)
    assert route.call_count == 1

    # another user's token is not held back
    assert HardcoverClient("token-b").fetch_audio_title(1) is None
    assert route.call_count == 2


@respx.mock
def test_hardcover_graphql_throttle_error_is_a_rate_limit():
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Throttled"}]})
    )

    with pytest.raises(HardcoverRateLimited):
        HardcoverClient("token-a").fetch_audio_title(1)

    assert ratelimit.check(HardcoverClient("token-a")._limit_key) is not None


@respx.mock
def test_hardcover_ordinary_graphql_error_is_not_a_rate_limit():
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "field 'x' not found"}]})
    )

    with pytest.raises(HardcoverError) as exc:
        HardcoverClient("token-a").fetch_audio_title(1)

    assert not isinstance(exc.value, HardcoverRateLimited)
    assert ratelimit.check(HardcoverClient("token-a")._limit_key) is None


@respx.mock
def test_limited_hardcover_skips_the_audio_title_step(clean_db, user):
    ratelimit.trip(HardcoverClient(user.hardcover_token)._limit_key, 60)
    book = make_book(clean_db, "Four: A Divergent Collection", "Veronica Roth")
    abb = mock_abb({"veronica roth four"})
    hardcover = mock_audio_title("Four: A Divergent Story Collection")

    found = search_releases(book, user)

    assert hardcover.call_count == 0
    assert found.query == "Veronica Roth Four"


@respx.mock
def test_sync_during_a_cooldown_fails_without_a_request(db_session, user: User):
    ratelimit.trip(HardcoverClient(user.hardcover_token)._limit_key, 60)
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, json={"data": {}}))

    with pytest.raises(HardcoverRateLimited):
        run_sync_for_user(user.id)

    assert route.call_count == 0
    db_session.refresh(user)
    assert "rate limiting" in user.last_sync_result


# -- release picker --------------------------------------------------------


@respx.mock
def test_picker_offers_a_search_box_prefilled_with_the_last_query(client, clean_db):
    book = make_book(clean_db, "Coraline", "Neil Gaiman")
    mock_abb()

    response = client.get(f"/books/{book.id}/releases")

    assert "No releases found" in response.text
    assert 'name="q" value="Coraline"' in response.text
    assert "Searched for “Coraline”" in response.text


@respx.mock
def test_picker_runs_a_typed_search(client, clean_db):
    book = make_book(clean_db, "Coraline", "Neil Gaiman")
    abb = mock_abb({"coraline full cast"})

    response = client.get(f"/books/{book.id}/releases", params={"q": "Coraline full cast"})

    assert searched(abb) == ["coraline full cast"]
    assert "Andy Weir - Project Hail Mary" in response.text  # the fixture's hits


@respx.mock
def test_typed_search_keeps_the_add_edition_context(client, clean_db):
    book = make_book(clean_db, "Coraline", "Neil Gaiman")
    clean_db.add(Edition(book=book, label="Neil Gaiman", library_path="/x", download_state=DownloadState.IMPORTED))
    clean_db.commit()
    mock_abb({"coraline full cast"})

    response = client.get(
        f"/books/{book.id}/releases",
        params={"q": "Coraline full cast", "add_edition": "1", "edition_label": "Full Cast",
                "hc_edition": "42", "narrator": "Cast", "detail": "1"},
    )

    # the available book's guard is skipped, and the grab form carries the edition
    assert "already available" not in response.text
    assert 'id="add-edition-options"' in response.text
    assert 'name="edition_label" value="Full Cast"' in response.text
    assert 'name="hc_edition" value="42"' in response.text
    assert 'name="detail" value="1"' in response.text


@respx.mock
def test_add_edition_without_a_label_keeps_the_book_guard(client, clean_db):
    book = make_book(clean_db, "Coraline", "Neil Gaiman")
    clean_db.add(Edition(book=book, label="", library_path="/x", download_state=DownloadState.IMPORTED))
    clean_db.commit()

    response = client.get(f"/books/{book.id}/releases", params={"add_edition": "1"})

    assert "already available" in response.text


@respx.mock
def test_picker_reports_a_rate_limit(client, clean_db):
    book = make_book(clean_db, "Coraline", "Neil Gaiman")
    respx.get(f"{ABB}/").mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))

    response = client.get(f"/books/{book.id}/releases")

    assert "AudioBookBay is limiting requests — try again in 30s." in response.text
    assert "Search failed" not in response.text
