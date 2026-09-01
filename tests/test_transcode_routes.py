"""The transcode UI: where the control appears, what the confirm dialog says,
and that queuing/cancelling go through the guards."""

import pytest

from app.models import TranscodeJob, TranscodeState
from app.services import transcode as service
from tests.test_transcode_worker import (  # noqa: F401
    clean_db,
    edition,
    fake_ffmpeg,
    m4b_fixture,
    settings_with,
)


@pytest.fixture
def ready(edition, fake_ffmpeg, settings_with):  # noqa: F811
    """An MP3 edition with a working (stubbed) ffmpeg."""
    settings_with(fake_ffmpeg)
    return edition


def files_fragment(client, edition):
    response = client.get(f"/editions/{edition.id}/files")
    assert response.status_code == 200
    return response.text


# --- where the control appears ---------------------------------------------


def test_the_file_list_offers_the_conversion(client, ready):
    body = files_fragment(client, ready)

    assert "Convert to M4B" in body
    assert f'id="transcode-{ready.id}"' in body


def test_an_m4b_edition_is_not_offered_it(client, ready, m4b_fixture):  # noqa: F811
    """Nothing to convert, so nothing to nag about."""
    import shutil
    from pathlib import Path

    for mp3 in Path(ready.library_path).glob("*.mp3"):
        mp3.unlink()
    shutil.copy(m4b_fixture, Path(ready.library_path) / "book.m4b")

    assert "Convert to M4B" not in files_fragment(client, ready)


def test_a_missing_ffmpeg_is_explained_not_hidden(client, ready, monkeypatch):
    monkeypatch.setattr(service, "ffmpeg_available", lambda *a, **k: False)

    body = files_fragment(client, ready)

    assert "Convert to M4B" not in body
    assert "ffmpeg is not available" in body


# --- the confirm dialog ----------------------------------------------------


def test_confirm_dialog_spells_out_what_happens(client, ready):
    body = client.get(f"/editions/{ready.id}/transcode/confirm").text

    assert "3</strong> MP3 files" in body
    assert "Transcode Me.m4b" in body
    assert "64 kb/s" in body
    assert "deleted" in body
    assert "cannot be undone" in body


def test_confirm_dialog_names_the_chapter_sidecar(client, ready):
    from pathlib import Path

    (Path(ready.library_path) / "book.cue").write_text(
        'FILE "book.mp3" MP3\n TRACK 01 AUDIO\n  TITLE "One"\n  INDEX 01 00:00:00\n'
    )

    body = client.get(f"/editions/{ready.id}/transcode/confirm").text

    assert "book.cue" in body


def test_confirm_dialog_reports_a_block(client, ready, monkeypatch):
    monkeypatch.setattr(service, "ffmpeg_available", lambda *a, **k: False)

    body = client.get(f"/editions/{ready.id}/transcode/confirm").text

    assert "ffmpeg is not available" in body
    assert "Convert</button>" not in body


# --- queuing ---------------------------------------------------------------


def test_starting_queues_exactly_one_job(client, ready, clean_db):  # noqa: F811
    response = client.post(f"/editions/{ready.id}/transcode")

    assert response.status_code == 200
    jobs = clean_db.query(TranscodeJob).all()
    assert [j.state for j in jobs] == [TranscodeState.QUEUED]
    # the modal closes and the panel swaps itself in out of band
    assert '<div id="modal"></div>' in response.text
    assert "hx-swap-oob" in response.text


def test_a_second_start_is_refused(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")

    body = client.post(f"/editions/{ready.id}/transcode").text

    assert "already being converted" in body
    assert clean_db.query(TranscodeJob).count() == 1


def test_a_queued_job_shows_progress_not_a_button(client, ready):
    client.post(f"/editions/{ready.id}/transcode")

    body = client.get(f"/editions/{ready.id}/transcode").text

    assert "Waiting to convert" in body
    assert "Convert to M4B" not in body
    assert "hx-trigger=\"every 2s\"" in body


def test_the_running_poll_does_not_re_examine_the_files(client, ready, monkeypatch):
    """This runs every two seconds for the length of the encode, and
    `mp3_sources` is an rglob plus a header read per file — hundreds of opens
    on the disk ffmpeg has busy, to answer a question the running job settles."""
    client.post(f"/editions/{ready.id}/transcode")

    def refuse(*args, **kwargs):
        raise AssertionError("the poll probed the folder while a job was active")

    monkeypatch.setattr("app.routes.transcode.mp3_sources", refuse)

    assert "Waiting to convert" in client.get(f"/editions/{ready.id}/transcode").text


def test_starting_needs_a_login(anon_client, ready):
    response = anon_client.post(f"/editions/{ready.id}/transcode", follow_redirects=False)
    assert response.status_code in (302, 303, 401, 403)


# --- cancel and dismiss ----------------------------------------------------


def test_cancel_marks_the_job_not_the_files(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")
    job = clean_db.query(TranscodeJob).one()

    body = client.post(f"/transcodes/{job.id}/cancel", headers={"hx-request": "true"}).text
    clean_db.refresh(job)

    # the worker owns the transition; the request only asks
    assert job.cancel_requested is True
    assert job.state == TranscodeState.QUEUED
    assert "stopping" in body


def test_dismiss_removes_a_finished_job(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")
    job = clean_db.query(TranscodeJob).one()
    job.state = TranscodeState.FAILED
    job.error = "it went wrong"
    clean_db.commit()

    client.post(f"/transcodes/{job.id}/dismiss", follow_redirects=False)

    assert clean_db.query(TranscodeJob).count() == 0


def test_dismiss_refuses_a_running_job(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")
    job = clean_db.query(TranscodeJob).one()
    job.state = TranscodeState.RUNNING
    clean_db.commit()

    response = client.post(f"/transcodes/{job.id}/dismiss", follow_redirects=False)

    assert response.status_code == 409
    assert clean_db.query(TranscodeJob).count() == 1


# --- activity page ---------------------------------------------------------


def test_activity_lists_a_running_conversion(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")
    job = clean_db.query(TranscodeJob).one()
    job.state = TranscodeState.RUNNING
    job.progress = 42.0
    job.source_count = 3
    clean_db.commit()

    body = client.get("/activity").text

    assert "Converting to M4B" in body
    assert "42%" in body
    assert "Transcode Me" in body


def test_activity_lists_a_failed_conversion(client, ready, clean_db):  # noqa: F811
    client.post(f"/editions/{ready.id}/transcode")
    job = clean_db.query(TranscodeJob).one()
    job.state = TranscodeState.FAILED
    job.error = "ffmpeg fell over"
    clean_db.commit()

    body = client.get("/activity").text

    assert "ffmpeg fell over" in body
    assert "The MP3 files were left alone." in body
