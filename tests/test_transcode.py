"""Chapter sources, bitrate selection and the ffmpeg command line.

The encode itself is verified in test_transcode_worker.py against a stubbed
ffmpeg; these are the pure parts.
"""

import json

import pytest

from app.models import AudioFile, Author, Book, Edition
from app.services.transcode import (
    SourceFile,
    build_command,
    chapter_plan,
    has_real_embedded_chapters,
    measure_durations,
    normalize_chapters,
    output_layout,
    parse_abs_metadata,
    parse_bitrate,
    parse_chapters_txt,
    parse_cue,
    parse_ffmetadata,
    parse_progress,
    sidecar_chapters,
    target_bitrate,
    write_ffmetadata,
)

FAKE_FFMPEG = """#!/bin/sh
# Answers a decode-measure pass with the duration encoded in the file's name.
for arg in "$@"; do
  case "$arg" in
    *.mp3) name=$(basename "$arg" .mp3) ;;
  esac
done
echo "out_time_us=${name}000000"
echo "progress=end"
"""


@pytest.fixture
def fake_ffmpeg(tmp_path):
    """A stand-in ffmpeg that reports each input's duration as its filename,
    so `5.mp3` measures 5 seconds."""
    path = tmp_path / "fake-ffmpeg"
    path.write_text(FAKE_FFMPEG)
    path.chmod(0o755)
    return str(path)

SINGLE_FILE_CUE = """PERFORMER "J.K. Rowling"
TITLE "Chamber of Secrets"
FILE "book.mp3" MP3
  TRACK 01 AUDIO
    TITLE "The Worst Birthday"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Dobby's Warning"
    INDEX 01 12:33:15
"""

PER_FILE_CUE = """FILE "01 - one.mp3" MP3
  TRACK 01 AUDIO
    TITLE "Opening"
    INDEX 01 00:00:00
FILE "02 - two.mp3" MP3
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 01 00:05:00
"""


def source(path, duration=60.0, bitrate=128_000, channels=2, sample_rate=44_100):
    return SourceFile(
        path=path, duration=duration, bitrate=bitrate,
        channels=channels, sample_rate=sample_rate,
    )


def make_edition(files, title="Test Book"):
    """An in-memory edition; nothing here touches the database."""
    book = Book(hardcover_id=1, title=title, author=Author(name="Test Author"))
    edition = Edition(book=book, library_path="/audiobooks/Test Author/Test Book")
    for index, (rel_path, duration, chapters) in enumerate(files, start=1):
        edition.audio_files.append(
            AudioFile(
                index=index, rel_path=rel_path, size=1, mtime_ms=0,
                mime_type="audio/mpeg", duration=duration,
                chapters_json=json.dumps(chapters) if chapters else None,
            )
        )
    return edition


# --- cue -------------------------------------------------------------------


def test_cue_single_file_times_are_absolute():
    chapters = parse_cue(SINGLE_FILE_CUE)

    assert [c["title"] for c in chapters] == ["The Worst Birthday", "Dobby's Warning"]
    # 12:33:15 is 12m 33s and 15 frames of 75 to the second
    assert chapters[1]["start"] == pytest.approx(12 * 60 + 33 + 15 / 75)


def test_cue_header_title_is_not_a_chapter():
    """The sheet's own TITLE precedes the first TRACK and must not leak."""
    assert parse_cue(SINGLE_FILE_CUE)[0]["title"] == "The Worst Birthday"


def test_cue_per_file_times_are_offset_by_their_file():
    chapters = parse_cue(PER_FILE_CUE, {"01 - one": 0.0, "02 - two": 300.0})

    # cue times are MM:SS:FF, so the second file's "00:05:00" is 5 seconds in
    assert [c["start"] for c in chapters] == [0.0, 305.0]


def test_cue_with_an_unknown_file_is_abandoned():
    """A misplaced offset silently scatters every chapter after it, so an
    unresolvable FILE drops the whole sheet rather than guessing."""
    assert parse_cue(PER_FILE_CUE, {"01 - one": 0.0}) is None


def test_cue_ignores_the_pregap_index():
    cue = 'FILE "book.mp3" MP3\n TRACK 01 AUDIO\n  TITLE "One"\n  INDEX 00 00:00:00\n  INDEX 01 00:02:00\n'
    assert [c["start"] for c in parse_cue(cue)] == [2.0]


def test_cue_without_quotes():
    cue = "FILE book.mp3 MP3\n TRACK 01 AUDIO\n  TITLE One\n  INDEX 01 00:01:00\n"
    assert parse_cue(cue) == [{"start": 1.0, "title": "One"}]


def test_cue_with_nothing_usable():
    assert parse_cue("REM COMMENT nothing here\n") is None


# --- other sidecars --------------------------------------------------------


@pytest.mark.parametrize(
    "line, start, title",
    [
        ("00:00:00.000 Prologue", 0.0, "Prologue"),
        ("00:12:33.500 Chapter One", 753.5, "Chapter One"),
        ("12:33 Chapter One", 753.0, "Chapter One"),
        ("- 01:00 Dash prefix", 60.0, "Dash prefix"),
        ("[00:30] Bracketed", 30.0, "Bracketed"),
        ("1:02:03 - With a separator", 3723.0, "With a separator"),
    ],
)
def test_chapters_txt_lines(line, start, title):
    assert parse_chapters_txt(line) == [{"start": start, "title": title}]


def test_chapters_txt_skips_prose():
    text = "Ripped by someone\n00:00 Prologue\nnot a chapter line\n05:00 Chapter One\n"
    assert [c["title"] for c in parse_chapters_txt(text)] == ["Prologue", "Chapter One"]


def test_ffmetadata_honours_its_timebase():
    text = (
        ";FFMETADATA1\ntitle=Book\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=5000\ntitle=Prologue\n"
        "[CHAPTER]\nTIMEBASE=1/1000000000\nSTART=5000000000\nEND=9000000000\ntitle=One\n"
    )

    assert parse_ffmetadata(text) == [
        {"start": 0.0, "title": "Prologue"},
        {"start": 5.0, "title": "One"},
    ]


def test_ffmetadata_needs_its_header():
    assert parse_ffmetadata("[CHAPTER]\nSTART=0\ntitle=No header\n") is None


def test_abs_metadata_chapters():
    text = json.dumps({"title": "Book", "chapters": [
        {"id": 0, "start": 0, "end": 12.5, "title": "Prologue"},
        {"id": 1, "start": 12.5, "end": 30, "title": "One"},
    ]})

    assert [c["start"] for c in parse_abs_metadata(text)] == [0.0, 12.5]


def test_abs_metadata_without_chapters():
    assert parse_abs_metadata(json.dumps({"title": "Book"})) is None
    assert parse_abs_metadata("not json") is None


def test_sidecar_precedence_prefers_the_cue(tmp_path):
    (tmp_path / "book.cue").write_text(SINGLE_FILE_CUE)
    (tmp_path / "chapters.txt").write_text("00:00 From the text file\n")

    chapters, path = sidecar_chapters(tmp_path, {})

    assert path.name == "book.cue"
    assert chapters[0]["title"] == "The Worst Birthday"


def test_sidecar_falls_through_an_unusable_cue(tmp_path):
    (tmp_path / "book.cue").write_text(PER_FILE_CUE)  # files we cannot place
    (tmp_path / "Chapters.txt").write_text("00:00 From the text file\n")

    chapters, path = sidecar_chapters(tmp_path, {})

    assert path.name == "Chapters.txt"  # matched case-insensitively
    assert chapters[0]["title"] == "From the text file"


def test_sidecar_prefers_the_shallowest(tmp_path):
    (tmp_path / "CD1").mkdir()
    (tmp_path / "CD1" / "disc.cue").write_text('FILE "a.mp3" MP3\n TRACK 01 AUDIO\n  TITLE Deep\n  INDEX 01 00:01:00\n')
    (tmp_path / "book.cue").write_text(SINGLE_FILE_CUE)

    _, path = sidecar_chapters(tmp_path, {})

    assert path.name == "book.cue"


def test_no_sidecars_at_all(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"img")
    assert sidecar_chapters(tmp_path, {}) is None


# --- choosing between embedded and sidecar ---------------------------------


def test_one_chapter_per_file_is_not_real_chapter_data():
    edition = make_edition([
        ("01.mp3", 60.0, [{"id": 0, "start": 0.0, "end": 60.0, "title": "One"}]),
        ("02.mp3", 60.0, [{"id": 0, "start": 0.0, "end": 60.0, "title": "Two"}]),
    ])
    assert has_real_embedded_chapters(edition) is False


def test_a_file_with_several_chapters_is():
    edition = make_edition([
        ("01.mp3", 60.0, [{"id": 0, "start": 0.0, "end": 30.0, "title": "One"},
                          {"id": 1, "start": 30.0, "end": 60.0, "title": "Two"}]),
        ("02.mp3", 60.0, None),
    ])
    assert has_real_embedded_chapters(edition) is True


def test_plan_prefers_embedded_chapters_over_a_sidecar(tmp_path):
    edition = make_edition([
        ("01.mp3", 60.0, [{"id": 0, "start": 0.0, "end": 30.0, "title": "Embedded one"},
                          {"id": 1, "start": 30.0, "end": 60.0, "title": "Embedded two"}]),
    ])
    edition.library_path = str(tmp_path)
    (tmp_path / "book.cue").write_text(SINGLE_FILE_CUE)

    chapters, sidecar = chapter_plan(edition, [60.0])

    assert [c["title"] for c in chapters] == ["Embedded one", "Embedded two"]
    assert sidecar is None


def test_plan_uses_a_sidecar_when_embedded_data_is_trivial(tmp_path):
    """A hand-made cue must not be shadowed by one trivial chapter per file."""
    edition = make_edition([
        ("01.mp3", 400.0, [{"id": 0, "start": 0.0, "end": 400.0, "title": "01"}]),
        ("02.mp3", 400.0, [{"id": 0, "start": 0.0, "end": 400.0, "title": "02"}]),
    ])
    edition.library_path = str(tmp_path)
    (tmp_path / "book.cue").write_text(SINGLE_FILE_CUE)

    chapters, sidecar = chapter_plan(edition, [400.0, 400.0])

    assert [c["title"] for c in chapters] == ["The Worst Birthday", "Dobby's Warning"]
    assert sidecar.name == "book.cue"


def test_plan_falls_back_to_the_abs_chapter_list(tmp_path):
    """No sidecar, no embedded data: exactly what the apps already show."""
    edition = make_edition([("01 - Opening.mp3", 60.0, None), ("02 - Second.mp3", 30.0, None)])
    edition.library_path = str(tmp_path)

    chapters, sidecar = chapter_plan(edition, [60.0, 30.0])

    assert [c["title"] for c in chapters] == ["01 - Opening", "02 - Second"]
    assert [c["start"] for c in chapters] == [0.0, 60.0]
    assert sidecar is None


def test_normalize_closes_ends_and_drops_overruns():
    chapters = normalize_chapters(
        [{"start": 30.0, "title": "Two"},
         {"start": 0.0, "title": "One"},
         {"start": 30.0, "title": "Repeat at the same instant"},
         {"start": 500.0, "title": "Past the end"},
         {"start": 60.0, "title": ""}],
        total=90.0,
    )

    assert [c["title"] for c in chapters] == ["One", "Two", "Chapter 3"]
    assert [c["start"] for c in chapters] == [0.0, 30.0, 60.0]
    assert [c["end"] for c in chapters] == [30.0, 60.0, 90.0]
    assert [c["id"] for c in chapters] == [0, 1, 2]


def test_write_ffmetadata_round_trips(tmp_path):
    chapters = normalize_chapters(
        [{"start": 0.0, "title": "Prologue"}, {"start": 5.0, "title": "One"}], total=16.0
    )
    path = tmp_path / "chapters.ffmeta"

    write_ffmetadata(chapters, path)

    assert parse_ffmetadata(path.read_text()) == [
        {"start": 0.0, "title": "Prologue"},
        {"start": 5.0, "title": "One"},
    ]


def test_write_ffmetadata_escapes_metacharacters(tmp_path):
    path = tmp_path / "chapters.ffmeta"
    write_ffmetadata(
        normalize_chapters([{"start": 0.0, "title": "A=B; #1\nsecond line"}], 10.0), path
    )

    text = path.read_text()
    assert "title=A\\=B\\; \\#1 second line" in text
    assert len(parse_ffmetadata(text)) == 1


# --- measured durations ----------------------------------------------------


def test_measure_durations_uses_ffmpeg_not_the_tags(tmp_path, fake_ffmpeg):
    """Tag durations are systematically long — they count the encoder delay and
    padding a gapless decoder trims (measured at ~0.8% per file). Harmless
    while each file is its own track; minutes of chapter drift once they are
    one file."""
    sources = [
        source(tmp_path / "5.mp3", duration=5.04),  # what the tag claims
        source(tmp_path / "7.mp3", duration=7.05),
    ]

    assert measure_durations(sources, fake_ffmpeg) == [5.0, 7.0]


def test_measure_durations_falls_back_when_ffmpeg_fails(tmp_path):
    """A file we cannot measure keeps its tag duration rather than failing the
    whole job."""
    broken = tmp_path / "broken-ffmpeg"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)

    assert measure_durations([source(tmp_path / "a.mp3", duration=12.5)], str(broken)) == [12.5]


def test_plan_places_chapters_on_measured_boundaries(tmp_path):
    """The whole point of measuring: a per-file chapter list lands on the
    boundaries the concatenated audio actually has."""
    edition = make_edition([("01 - One.mp3", 5.04, None), ("02 - Two.mp3", 7.05, None)])
    edition.library_path = str(tmp_path)

    chapters, _ = chapter_plan(edition, [5.0, 7.0])

    assert [c["start"] for c in chapters] == [0.0, 5.0]
    assert chapters[-1]["end"] == 12.0


# --- encoding parameters ---------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [("64k", 64_000), ("64000", 64_000), ("64", 64_000), (" 96K ", 96_000), ("128kbps", 128_000)],
)
def test_parse_bitrate(value, expected):
    assert parse_bitrate(value) == expected


def test_target_bitrate_is_capped_by_the_source():
    sources = [source("a.mp3", bitrate=32_000), source("b.mp3", bitrate=32_000)]
    assert target_bitrate(sources, "64k") == 32_000


def test_target_bitrate_halves_for_a_mono_book():
    sources = [source("a.mp3", channels=1), source("b.mp3", channels=1)]
    assert target_bitrate(sources, "64k") == 32_000


def test_target_bitrate_keeps_stereo_when_any_part_is():
    sources = [source("a.mp3", channels=1), source("b.mp3", channels=2)]
    assert target_bitrate(sources, "64k") == 64_000


def test_target_bitrate_survives_unreadable_bitrates():
    assert target_bitrate([source("a.mp3", bitrate=None)], "64k") == 64_000


def test_output_layout_takes_the_best_of_the_sources():
    sources = [source("a.mp3", channels=1, sample_rate=22_050), source("b.mp3", sample_rate=44_100)]
    assert output_layout(sources) == ("stereo", 44_100)


def test_output_layout_never_upsamples_past_48k():
    assert output_layout([source("a.mp3", sample_rate=96_000)])[1] == 48_000


# --- the command line ------------------------------------------------------


def test_command_shape(tmp_path):
    sources = [source(tmp_path / "01.mp3"), source(tmp_path / "02.mp3", channels=1, sample_rate=22_050)]
    command = build_command(
        sources, tmp_path / "c.ffmeta", tmp_path / ".out.m4b.part", 64_000,
        {"title": "Book", "artist": "Author"},
    )

    # -f ipod is mandatory: the output name has no extension ffmpeg knows
    assert "-f" in command and command[command.index("-f") + 1] == "ipod"
    # the metadata file is the last input, and both maps point at it
    assert command[command.index("-map_metadata") + 1] == "2"
    assert command[command.index("-map_chapters") + 1] == "2"
    filters = command[command.index("-filter_complex") + 1]
    # every input is normalized before concatenation, which is what lets a
    # mixed-rate, mixed-channel book join cleanly
    assert filters.count("aformat=sample_rates=44100:channel_layouts=stereo") == 2
    assert "[a0][a1]concat=n=2:v=0:a=1[out]" in filters
    assert command[command.index("-b:a") + 1] == "64000"
    assert command[-1] == str(tmp_path / ".out.m4b.part")


def test_command_uses_a_list_file_for_a_huge_book(tmp_path):
    sources = [source(tmp_path / f"{i}.mp3") for i in range(3)]
    command = build_command(
        sources, tmp_path / "c.ffmeta", tmp_path / "out.part", 64_000, {},
        list_path=tmp_path / "list.txt",
    )

    assert "-filter_complex" not in command
    assert command[command.index("-f") + 1] == "concat"
    assert command[command.index("-map_metadata") + 1] == "1"


def test_command_omits_empty_tags(tmp_path):
    command = build_command(
        [source(tmp_path / "a.mp3")], tmp_path / "c.ffmeta", tmp_path / "o.part", 64_000,
        {"title": "Book", "composer": ""},
    )
    assert "composer=" not in " ".join(command)


@pytest.mark.parametrize(
    "line, expected",
    [("out_time_us=12500000", 12.5), ("out_time_ms=12500000", 12.5),
     ("progress=continue", None), ("out_time_us=N/A", None), ("", None)],
)
def test_parse_progress(line, expected):
    assert parse_progress(line) == expected
