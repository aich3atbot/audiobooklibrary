"""The destructive half: what run_job does to the folder, and in what order.

ffmpeg is stubbed. The real binary is verified separately (see plan.md
"Verified live"); what matters here is that nothing is deleted unless a good
file was produced first, and that every failure leaves the edition untouched.
"""

import shutil
import sys

import pytest

from app.models import (
    AppState,
    AudioFile,
    Author,
    Book,
    DownloadState,
    Edition,
    MediaProgress,
    Release,
    Series,
    TranscodeJob,
    TranscodeState,
)
from app.services import transcode
from app.services.transcode import (
    mp3_sources,
    queue_job,
    recover_interrupted_jobs,
    run_job,
    run_next_job,
    transcode_blocked,
)
from tests.test_audio_meta import SILENT_MP3_FRAME, write_mp3

# Stands in for ffmpeg, and behaves like it in the ways that matter:
#
#   - the measure pass (-f null) reports each input's duration, which these
#     fixtures encode in the filename ("5.mp3" is five seconds);
#   - the encode writes a *real* MP4 whose mvhd duration matches what it says
#     it encoded, so the validation step is genuinely exercised rather than
#     waved through. FAKE_SCALE makes it lie, which is how a truncated encode
#     is tested; FAKE_LINES makes it emit enough progress to reach a cancel
#     checkpoint.
FAKE_FFMPEG = '''#!{python}
import os, sys, time

args = sys.argv[1:]


def box(kind, *parts):
    payload = b"".join(parts)
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def u32(value):
    return value.to_bytes(4, "big")


def m4b(duration, timescale=1000):
    """ftyp + moov/mvhd — enough for mutagen to report a duration."""
    mvhd = box(
        b"mvhd",
        b"\\x00" * 4 + u32(0) + u32(0) + u32(timescale) + u32(int(duration * timescale))
        + u32(0x00010000) + b"\\x01\\x00" + b"\\x00" * 10 + b"\\x00" * 36
        + b"\\x00" * 24 + u32(2),
    )
    return box(b"ftyp", b"M4A \\x00\\x00\\x00\\x00") + box(b"moov", mvhd)


def emit(seconds):
    lines = int(os.environ.get("FAKE_LINES", "1"))
    for index in range(lines):
        print("out_time_us=%d" % int(seconds * 1e6 * (index + 1) / lines), flush=True)
        if lines > 1:
            time.sleep(0.01)
    print("progress=end", flush=True)


if "null" in args:  # the duration-measuring pass
    source = [a for a in args if a.endswith(".mp3")][-1]
    emit(float(os.path.basename(source)[:-len(".mp3")]))
    sys.exit(0)

meta = [a for a in args if a.endswith(".ffmeta")][-1]
with open(meta) as handle:
    end = max(int(line.split("=")[1]) for line in handle if line.startswith("END="))
seconds = end / 1000 * float(os.environ.get("FAKE_SCALE", "1"))
with open(args[-1], "wb") as handle:
    handle.write(m4b(seconds))
emit(seconds)
'''

FAILING_FFMPEG = "#!/bin/sh\necho 'something exploded' >&2\nexit 1\n"

# Writes far more to stderr than a pipe holds (64 KB) before finishing, the way
# ffmpeg does on MP3s with damaged frames — one real case emitted 132 KB of
# "Header missing". If stderr is ever a pipe again this stops dead mid-encode.
NOISY_FFMPEG = '''#!{python}
import os, sys

args = sys.argv[1:]
for _ in range(4000):
    print("[mp3float @ 0x0] Header missing", file=sys.stderr)

if "null" in args:
    source = [a for a in args if a.endswith(".mp3")][-1]
    print("out_time_us=%d" % int(float(os.path.basename(source)[:-4]) * 1e6))
    print("progress=end")
    sys.exit(0)

meta = [a for a in args if a.endswith(".ffmeta")][-1]
with open(meta) as handle:
    end = max(int(line.split("=")[1]) for line in handle if line.startswith("END="))
seconds = end / 1000


def box(kind, *parts):
    payload = b"".join(parts)
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def u32(value):
    return value.to_bytes(4, "big")


mvhd = box(b"mvhd", b"\\x00" * 4 + u32(0) + u32(0) + u32(1000) + u32(int(seconds * 1000))
           + u32(0x00010000) + b"\\x01\\x00" + b"\\x00" * 10 + b"\\x00" * 36
           + b"\\x00" * 24 + u32(2))
with open(args[-1], "wb") as handle:
    handle.write(box(b"ftyp", b"M4A \\x00\\x00\\x00\\x00") + box(b"moov", mvhd))
for _ in range(4000):
    print("[mp3float @ 0x0] Header missing", file=sys.stderr)
print("out_time_us=%d" % int(seconds * 1e6))
print("progress=end")
'''


def write_stub(path, body=None):
    path.write_text((body or FAKE_FFMPEG).format(python=sys.executable))
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def clean_db(db_session):
    for model in (TranscodeJob, AudioFile, MediaProgress, Release, Edition, Book,
                  Author, Series, AppState):
        db_session.query(model).delete()
    db_session.commit()
    return db_session


@pytest.fixture
def m4b_fixture(tmp_path_factory):
    """A real (tiny) m4b, for the "this folder holds other audio" checks."""
    from tests.test_mp4_chapters import nero_m4b

    path = tmp_path_factory.mktemp("fixture") / "sample.m4b"
    return nero_m4b(path, [(0.0, "One"), (5.0, "Two")])


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "ffmpeg_available", lambda *a, **k: True)
    return write_stub(tmp_path / "fake-ffmpeg")


@pytest.fixture
def settings_with(test_settings, monkeypatch):
    def apply(ffmpeg):
        monkeypatch.setattr(test_settings, "ffmpeg_path", ffmpeg)
        return test_settings
    return apply


@pytest.fixture
def edition(clean_db, test_settings):
    """An imported edition of three MP3s named for their duration, so the
    stub's measure pass has something to report."""
    lib = test_settings.library_dir / "Test Author" / "Transcode Me"
    if lib.exists():
        shutil.rmtree(lib)
    author = Author(hardcover_id=900, name="Test Author")
    book = Book(hardcover_id=9000, title="Transcode Me", author=author)
    row = Edition(book=book, download_state=DownloadState.IMPORTED, library_path=str(lib))
    clean_db.add_all([book, row])
    for name in ("5.mp3", "4.mp3", "3.mp3"):
        write_mp3(lib / name, frames=100)
    (lib / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 200)
    clean_db.commit()
    from app.services.audio_meta import scan_edition_audio

    scan_edition_audio(clean_db, row)
    return row


def library_files(edition):
    from pathlib import Path

    root = Path(edition.library_path)
    return sorted(p.name for p in root.rglob("*") if p.is_file())


# --- eligibility -----------------------------------------------------------


def test_mp3_only_folder_is_transcodable(edition, clean_db, fake_ffmpeg, settings_with):
    settings_with(fake_ffmpeg)
    assert transcode_blocked(clean_db, edition) is None


def test_a_folder_with_other_audio_is_not(edition, clean_db, fake_ffmpeg, settings_with, m4b_fixture):
    settings_with(fake_ffmpeg)
    from pathlib import Path

    shutil.copy(m4b_fixture, Path(edition.library_path) / "already.m4b")

    assert "all MP3" in transcode_blocked(clean_db, edition)


def test_a_mislabelled_mp3_is_not(edition, m4b_fixture):
    """Identified by contents: an m4b renamed .mp3 is not a source we can
    concatenate as one."""
    from pathlib import Path

    root = Path(edition.library_path)
    shutil.copy(m4b_fixture, root / "sneaky.mp3")

    assert mp3_sources(root) is None


def test_a_download_in_flight_blocks_it(edition, clean_db, fake_ffmpeg, settings_with):
    settings_with(fake_ffmpeg)
    clean_db.add(Release(edition_id=edition.id, guid="g", title="t", status="downloading"))
    clean_db.commit()

    assert "download in flight" in transcode_blocked(clean_db, edition)


def test_an_active_job_blocks_it(edition, clean_db, fake_ffmpeg, settings_with):
    settings_with(fake_ffmpeg)
    queue_job(clean_db, edition, None)

    assert "already being converted" in transcode_blocked(clean_db, edition)


def test_no_ffmpeg_blocks_it(edition, clean_db, monkeypatch):
    monkeypatch.setattr(transcode, "ffmpeg_available", lambda *a, **k: False)
    assert "ffmpeg is not available" in transcode_blocked(clean_db, edition)


# --- the happy path --------------------------------------------------------


def test_successful_transcode_replaces_the_mp3s(edition, clean_db, fake_ffmpeg, settings_with):
    settings_with(fake_ffmpeg)
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is True

    assert library_files(edition) == ["Transcode Me.m4b", "cover.jpg"]
    assert job.state == TranscodeState.DONE
    assert job.progress == 100.0
    assert job.source_count == 3
    assert job.error is None
    assert job.output_path.endswith("Transcode Me.m4b")
    # the audio rows are rebuilt around the new single file
    assert [f.rel_path for f in edition.audio_files] == ["Transcode Me.m4b"]


def test_successful_transcode_removes_the_consumed_cue(edition, clean_db, fake_ffmpeg, settings_with):
    """Its chapters are inside the m4b now, and it names files that are gone."""
    from pathlib import Path

    settings_with(fake_ffmpeg)
    root = Path(edition.library_path)
    (root / "book.cue").write_text(
        'FILE "book.mp3" MP3\n TRACK 01 AUDIO\n  TITLE "One"\n  INDEX 01 00:00:00\n'
    )
    (root / "notes.nfo").write_text("keep me")

    run_job(clean_db, queue_job(clean_db, edition, None))

    assert library_files(edition) == ["Transcode Me.m4b", "cover.jpg", "notes.nfo"]


def test_a_consumed_metadata_json_is_kept(edition, clean_db, fake_ffmpeg, settings_with):
    """Audiobookshelf's metadata.json is a chapter source, but it is not *only*
    that — description, series and narrator ride along, and nothing here can
    write them back. Deleting it as a spent sidecar would lose them."""
    import json
    from pathlib import Path

    settings_with(fake_ffmpeg)
    root = Path(edition.library_path)
    (root / "metadata.json").write_text(json.dumps({
        "description": "Everything the m4b will not carry",
        "narrators": ["A Reader"],
        "chapters": [{"id": 0, "start": 0, "end": 12, "title": "One"}],
    }))

    assert run_job(clean_db, queue_job(clean_db, edition, None)) is True

    assert library_files(edition) == ["Transcode Me.m4b", "cover.jpg", "metadata.json"]
    assert "Everything the m4b will not carry" in (root / "metadata.json").read_text()


def test_emptied_disc_folders_are_pruned(clean_db, test_settings, fake_ffmpeg, settings_with):
    from pathlib import Path

    from app.services.audio_meta import scan_edition_audio

    settings_with(fake_ffmpeg)
    lib = test_settings.library_dir / "Test Author" / "Discs"
    if lib.exists():
        shutil.rmtree(lib)
    author = Author(hardcover_id=901, name="Test Author")
    book = Book(hardcover_id=9001, title="Discs", author=author)
    row = Edition(book=book, download_state=DownloadState.IMPORTED, library_path=str(lib))
    clean_db.add_all([book, row])
    write_mp3(lib / "CD1" / "5.mp3")
    write_mp3(lib / "CD2" / "4.mp3")
    clean_db.commit()
    scan_edition_audio(clean_db, row)

    run_job(clean_db, queue_job(clean_db, row, None))

    assert library_files(row) == ["Discs.m4b"]
    assert not (Path(lib) / "CD1").exists()


# --- failure paths ---------------------------------------------------------


def test_a_failed_encode_keeps_everything(edition, clean_db, tmp_path, settings_with):
    before = library_files(edition)
    settings_with(write_stub(tmp_path / "broken-ffmpeg", FAILING_FFMPEG))
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is False

    assert library_files(edition) == before
    assert job.state == TranscodeState.FAILED
    assert "something exploded" in job.error


def test_a_truncated_encode_is_rejected(edition, clean_db, fake_ffmpeg, settings_with, monkeypatch):
    """The file is playable but holds a tenth of the audio that went in — the
    one check standing between a truncated encode and deleting the originals."""
    before = library_files(edition)
    settings_with(fake_ffmpeg)
    monkeypatch.setenv("FAKE_SCALE", "0.1")
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is False

    assert library_files(edition) == before
    assert job.state == TranscodeState.FAILED
    assert "of audio" in job.error


def test_a_measured_book_keeps_the_flat_tolerance(edition, clean_db, fake_ffmpeg, settings_with, monkeypatch):
    """Every file measured, so a 10% shortfall on a 12-second book — 1.2s — is
    still a truncated encode and not slack owed to any estimate."""
    settings_with(fake_ffmpeg)
    monkeypatch.setenv("FAKE_SCALE", "0.9")
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is False

    assert "of audio" in job.error


def test_an_unmeasurable_file_widens_the_check_by_its_own_length(
    edition, clean_db, fake_ffmpeg, settings_with, monkeypatch
):
    """One file that ffmpeg could not measure keeps its tag duration, which
    runs ~0.8% long. The allowance is charged against that file's seconds
    alone: 500 estimated seconds buy 10s of slack, so an encode 3s short of the
    expected total is kept rather than binned as truncated — a correct encode
    of a multi-hour book used to be discarded over exactly this."""
    settings_with(fake_ffmpeg)
    monkeypatch.setattr(
        transcode, "measure_durations", lambda *a, **k: ([500.0, 4.0, 3.0], 500.0)
    )
    monkeypatch.setenv("FAKE_SCALE", "0.994")  # ~3s under the 507s we expect
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is True

    assert library_files(edition) == ["Transcode Me.m4b", "cover.jpg"]


def test_an_unplayable_output_is_rejected(edition, clean_db, tmp_path, settings_with):
    """ffmpeg exits 0 but what it wrote is not audio."""
    before = library_files(edition)
    stub = write_stub(
        tmp_path / "junk-ffmpeg",
        '#!/bin/sh\nfor a in "$@"; do out="$a"; done\n'
        'case "$*" in *"-f null"*) echo "out_time_us=5000000"; exit 0 ;; esac\n'
        'echo not-audio > "$out"\necho "out_time_us=12000000"\n',
    )
    settings_with(stub)
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is False

    assert library_files(edition) == before
    assert "not playable audio" in job.error


def test_a_missing_folder_fails_cleanly(edition, clean_db, fake_ffmpeg, settings_with):
    settings_with(fake_ffmpeg)
    edition.library_path = "/nonexistent/nowhere"
    clean_db.commit()
    job = queue_job(clean_db, edition, None)

    assert run_job(clean_db, job) is False
    assert job.state == TranscodeState.FAILED


def test_a_chatty_ffmpeg_does_not_deadlock(edition, clean_db, tmp_path, settings_with):
    """ffmpeg on MP3s with damaged frames writes megabytes of decoder errors.
    If stderr were a pipe it would fill at 64 KB and block the encode forever —
    which is exactly what happened on a real 61-minute book, frozen at 48%.

    Run in a thread so a regression fails the test instead of hanging the suite.
    """
    import threading

    settings_with(write_stub(tmp_path / "noisy-ffmpeg", NOISY_FFMPEG))
    job = queue_job(clean_db, edition, None)
    result = {}

    # daemon, so a regression fails this test in 60s rather than wedging the
    # whole suite at interpreter exit waiting for a thread that never returns
    worker = threading.Thread(
        target=lambda: result.update(ok=run_job(clean_db, job)), daemon=True
    )
    worker.start()
    worker.join(timeout=60)

    assert not worker.is_alive(), "the encode blocked on its own stderr"
    assert result.get("ok") is True
    assert library_files(edition) == ["Transcode Me.m4b", "cover.jpg"]


def test_temp_files_never_survive_a_failure(edition, clean_db, tmp_path, settings_with):
    settings_with(write_stub(tmp_path / "broken-ffmpeg", FAILING_FFMPEG))

    run_job(clean_db, queue_job(clean_db, edition, None))

    assert not [name for name in library_files(edition) if name.startswith(".")]


# --- cancel and recovery ---------------------------------------------------


def test_cancelling_a_running_encode_stops_it(edition, clean_db, fake_ffmpeg, settings_with, monkeypatch):
    """Cancel travels through the database, not a shared process handle: the
    worker is already reading a progress pipe, and notices there."""
    before = library_files(edition)
    settings_with(fake_ffmpeg)
    monkeypatch.setenv("FAKE_LINES", "80")  # enough to reach a checkpoint
    job = queue_job(clean_db, edition, None)
    measure = transcode.measure_durations

    def cancel_once_measured(*args, **kwargs):
        # the flag arrives after the measure pass, so only the encode loop can
        # be the one that notices it
        result = measure(*args, **kwargs)
        job.cancel_requested = True
        clean_db.commit()
        return result

    monkeypatch.setattr(transcode, "measure_durations", cancel_once_measured)

    assert run_job(clean_db, job) is False

    assert job.state == TranscodeState.CANCELLED
    assert job.error is None
    assert library_files(edition) == before


def test_cancelling_during_the_measure_pass_stops_before_the_encode(
    edition, clean_db, fake_ffmpeg, settings_with
):
    """The decode pass runs before ffmpeg is ever started and can be minutes of
    work on a long book, so it watches the flag too."""
    before = library_files(edition)
    settings_with(fake_ffmpeg)
    job = queue_job(clean_db, edition, None)
    job.cancel_requested = True
    clean_db.commit()

    assert run_job(clean_db, job) is False

    assert job.state == TranscodeState.CANCELLED
    assert job.error is None
    assert job.progress == 0.0  # never reached the encode
    assert library_files(edition) == before


def test_cancelling_a_queued_job_never_runs_it(edition, clean_db, fake_ffmpeg, settings_with):
    settings_with(fake_ffmpeg)
    job = queue_job(clean_db, edition, None)
    job.cancel_requested = True
    clean_db.commit()

    assert run_next_job() is True
    clean_db.refresh(job)

    assert job.state == TranscodeState.CANCELLED
    assert "Transcode Me.m4b" not in library_files(edition)


def test_a_job_left_running_by_a_restart_is_failed(edition, clean_db, fake_ffmpeg, settings_with):
    """The process that owned the encode is gone; it cannot be resumed, and
    its half-written temp file must not be left behind."""
    from pathlib import Path

    settings_with(fake_ffmpeg)
    job = queue_job(clean_db, edition, None)
    job.state = TranscodeState.RUNNING
    clean_db.commit()
    stray = Path(edition.library_path) / f".Transcode Me{transcode.TEMP_SUFFIX}"
    stray.write_bytes(b"half an encode")

    assert recover_interrupted_jobs() == 1
    clean_db.refresh(job)

    assert job.state == TranscodeState.FAILED
    assert "restart" in job.error
    assert not stray.exists()


def test_the_worker_reports_an_empty_queue(clean_db):
    assert run_next_job() is False
