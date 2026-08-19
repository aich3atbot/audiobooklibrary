"""The Activity page refreshing itself, and the watcher pacing that feeds it."""

import pytest

from app.routes.activity import ACTIVE_POLL_SECONDS, IDLE_POLL_SECONDS
from app.services.importer import ACTIVE_WATCH_SECONDS, watch_interval
from app.models import TranscodeJob, TranscodeState
from tests.test_transcode_worker import (  # noqa: F401
    clean_db,
    edition,
    fake_ffmpeg,
    m4b_fixture,
    settings_with,
)


@pytest.fixture
def ready(edition, fake_ffmpeg, settings_with):  # noqa: F811
    settings_with(fake_ffmpeg)
    return edition


def poll_interval(body: str) -> str:
    """The refresh interval the page just asked for."""
    marker = 'hx-trigger="every '
    start = body.index(marker) + len(marker)
    return body[start : body.index("[", start)]


# --- the fragment ----------------------------------------------------------


def test_a_browser_gets_the_whole_page(client, clean_db):  # noqa: F811
    body = client.get("/activity").text

    assert "<!doctype html" in body.lower()
    assert '<div id="activity"' in body
    assert "Recently imported" in body


def test_htmx_gets_only_the_fragment(client, clean_db):  # noqa: F811
    body = client.get("/activity", headers={"hx-request": "true"}).text

    assert "<!doctype html" not in body.lower()
    assert "<nav" not in body
    assert body.strip().startswith('<div id="activity"')
    # every section still comes along, so a finished download is seen moving
    # from one to the next
    for heading in ("Active downloads", "Needs attention", "Recently imported"):
        assert heading in body


def test_the_fragment_refreshes_itself(client, clean_db):  # noqa: F811
    body = client.get("/activity", headers={"hx-request": "true"}).text

    assert 'hx-get="/activity"' in body
    assert 'hx-swap="outerHTML"' in body


def test_typing_in_manual_import_suppresses_the_refresh(client, clean_db):  # noqa: F811
    """A swap mid-typing would collapse the disclosure and discard the folder
    name, so an open one (or any focused field) filters the tick out."""
    body = client.get("/activity", headers={"hx-request": "true"}).text

    assert "[!document.querySelector('#activity details[open], #activity :focus')]" in body


# --- the adaptive interval -------------------------------------------------


def test_an_idle_page_polls_slowly(client, clean_db):  # noqa: F811
    assert poll_interval(client.get("/activity").text) == f"{IDLE_POLL_SECONDS}s"


def test_a_running_conversion_speeds_it_up(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")

    assert poll_interval(client.get("/activity").text) == f"{ACTIVE_POLL_SECONDS}s"


def test_a_download_speeds_it_up(client, clean_db, edition):  # noqa: F811
    from app.models import Release

    clean_db.add(Release(edition_id=edition.id, guid="g", title="t", status="downloading"))
    clean_db.commit()

    assert poll_interval(client.get("/activity").text) == f"{ACTIVE_POLL_SECONDS}s"


def test_it_slows_down_again_when_the_work_finishes(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")
    job = clean_db.query(TranscodeJob).one()
    assert poll_interval(client.get("/activity").text) == f"{ACTIVE_POLL_SECONDS}s"

    job.state = TranscodeState.DONE
    clean_db.commit()

    assert poll_interval(client.get("/activity").text) == f"{IDLE_POLL_SECONDS}s"


# --- the watcher's own pacing ----------------------------------------------


def test_the_watcher_hurries_while_something_downloads(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "watch_interval_seconds", 30)

    assert watch_interval(active=1) == ACTIVE_WATCH_SECONDS
    assert watch_interval(active=0) == 30


def test_the_watcher_never_polls_slower_than_configured(test_settings, monkeypatch):
    """WATCH_INTERVAL_SECONDS stays the ceiling: an operator who asked for less
    chatter than the active rate keeps it."""
    monkeypatch.setattr(test_settings, "watch_interval_seconds", 5)

    assert watch_interval(active=1) == 5
    assert watch_interval(active=0) == 5


def test_a_watcher_pass_reports_what_is_active(clean_db, edition, test_settings):  # noqa: F811
    """The loop paces itself on this rather than asking the database again."""
    from app.models import Release
    from app.services.importer import scan_downloads_once

    test_settings.download_dir.mkdir(parents=True, exist_ok=True)
    clean_db.add(Release(edition_id=edition.id, guid="g", title="t", status="downloading"))
    clean_db.commit()

    assert scan_downloads_once()["active"] == 1


def test_a_pass_with_no_download_dir_reports_nothing_active(clean_db, edition, test_settings, monkeypatch):  # noqa: F811
    """Without a download directory there is nothing to watch, so the loop
    should fall back to its idle cadence rather than hurrying for nothing."""
    from app.models import Release
    from app.services.importer import scan_downloads_once

    monkeypatch.setattr(test_settings, "download_dir", test_settings.config_dir / "gone")
    clean_db.add(Release(edition_id=edition.id, guid="g", title="t", status="downloading"))
    clean_db.commit()

    assert scan_downloads_once()["active"] == 0
