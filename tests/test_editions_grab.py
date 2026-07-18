"""Hardcover editions fetch and the multi-edition grab flow: the add-edition
dialog, per-edition grabs and replaces, and imports landing in labelled
folders."""

import httpx
import pytest
import respx

from app.clients.hardcover import API_URL, HardcoverClient
from app.models import DownloadState, Edition, Release
from app.services.sync import get_state
from tests.test_downloads import (  # noqa: F401  (fixtures)
    DELUGE,
    GUID,
    HASH,
    book,
    clean_db,
    download_config,
    grab_data,
    imported_book,
    make_edition,
    mock_deluge,
    mock_details,
    mock_search,
)


def edition_row(id, narrators=("Stephen Fry",), users=5, **extra):
    contributions = [{"contribution": None, "author": {"id": 1, "name": "J.K. Rowling"}}]
    contributions += [
        {"contribution": "Narrator", "author": {"id": 100 + i, "name": n}}
        for i, n in enumerate(narrators)
    ]
    return {
        "id": id,
        "title": "Harry Potter and the Chamber of Secrets",
        "subtitle": None,
        "edition_format": "Audiobook",
        "asin": None,
        "isbn_13": None,
        "audio_seconds": 35000,
        "release_date": "2015-11-20",
        "users_count": users,
        "publisher": {"name": "Pottermore"},
        "contributions": contributions,
        **extra,
    }


def mock_editions(rows):
    return respx.post(API_URL).mock(
        return_value=httpx.Response(200, json={"data": {"editions": rows}})
    )


# --- fetch_editions ---------------------------------------------------------


@respx.mock
def test_fetch_editions_parses_narrators_and_labels():
    route = mock_editions([
        edition_row(1, narrators=("Stephen Fry",)),
        edition_row(2, narrators=("Simon Pegg", "Riz Ahmed", "Hugh Laurie", "Cush Jumbo")),
        edition_row(3, narrators=()),
        # role strings vary in the wild: lowercase, "Sprecher", null for authors
        {**edition_row(4, narrators=()), "contributions": [
            {"contribution": "narrator", "author": {"id": 1, "name": "Ann Saunders"}},
            {"contribution": "Sprecher", "author": {"id": 2, "name": "Rufus Beck"}},
        ]},
    ])

    with HardcoverClient("token") as client:
        editions = client.fetch_editions(429306)

    assert b"reading_format_id" in route.calls[0].request.content
    assert editions[0]["narrator"] == "Stephen Fry"
    assert editions[0]["default_label"] == "Stephen Fry"
    # a big ensemble defaults to "Full Cast" rather than a wall of names
    assert editions[1]["default_label"] == "Full Cast"
    assert editions[1]["narrators"][0] == "Simon Pegg"
    # no narrator data -> no default label (free-form input takes over)
    assert editions[2]["default_label"] == ""
    # role strings are matched case-insensitively incl. Reader/Sprecher
    assert editions[3]["narrators"] == ["Ann Saunders", "Rufus Beck"]


# --- the add-edition dialog -------------------------------------------------


@respx.mock
def test_new_edition_dialog_requires_label_for_existing_files(client, imported_book):
    mock_editions([edition_row(30920116)])

    response = client.get(f"/books/{imported_book.id}/editions/new")

    assert response.status_code == 200
    assert 'name="existing_label"' in response.text  # the unlabelled edition must be named
    assert "Stephen Fry" in response.text
    assert 'name="edition_label"' in response.text


@respx.mock
def test_new_edition_dialog_degrades_without_hardcover(client, imported_book):
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("down"))

    response = client.get(f"/books/{imported_book.id}/editions/new")

    assert response.status_code == 200
    assert "enter a label instead" in response.text
    assert 'name="edition_label"' in response.text


@respx.mock
def test_new_edition_submit_relabels_existing_and_shows_picker(
    client, clean_db, imported_book, test_settings
):
    mock_editions([edition_row(30920116)])
    mock_search()
    old_dir = test_settings.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown"

    response = client.post(
        f"/books/{imported_book.id}/editions/new",
        data={
            "existing_label": "Jim Dale",
            "hc_edition": "30920116",
            "hclabel_30920116": "Stephen Fry",
            "hcnarr_30920116": "Stephen Fry",
        },
    )

    assert response.status_code == 200
    # the existing files moved to their labelled folder immediately
    assert not old_dir.exists()
    labelled = test_settings.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown {Jim Dale}"
    assert (labelled / "Part 01.mp3").exists()
    clean_db.expire_all()
    existing = next(e for e in imported_book.editions if e.library_path)
    assert existing.label == "Jim Dale"
    # the release picker carries the new edition's identity for the grab
    assert 'name="add_edition" value="1"' in response.text
    assert 'name="edition_label" value="Stephen Fry"' in response.text
    assert 'name="hcnarr_30920116" value="Stephen Fry"' in response.text


@respx.mock
def test_new_edition_submit_requires_some_label(client, imported_book):
    mock_editions([edition_row(30920116)])

    response = client.post(
        f"/books/{imported_book.id}/editions/new",
        data={"existing_label": "Jim Dale"},
    )

    assert "Pick a Hardcover edition or enter a label" in response.text


@respx.mock
def test_new_edition_submit_requires_existing_label(client, imported_book):
    mock_editions([edition_row(30920116)])

    response = client.post(
        f"/books/{imported_book.id}/editions/new",
        data={"edition_label": "Stephen Fry"},
    )

    assert "existing files need a label" in response.text


@respx.mock
def test_new_edition_submit_rejects_same_label(client, imported_book):
    mock_editions([edition_row(30920116)])

    response = client.post(
        f"/books/{imported_book.id}/editions/new",
        data={"existing_label": "Stephen Fry", "edition_label": "Stephen Fry"},
    )

    assert "different label" in response.text


# --- grabbing another edition ----------------------------------------------


@respx.mock
def test_grab_add_edition_targets_the_labelled_edition(client, clean_db, imported_book):
    mock_details()
    mock_deluge()
    existing = imported_book.editions[0]
    existing.label = "Jim Dale"
    clean_db.commit()

    response = client.post(
        f"/books/{imported_book.id}/grab",
        data=grab_data(
            add_edition="1",
            edition_label="Stephen Fry",
            hc_edition="30920116",
            hcnarr_30920116="Stephen Fry",
        ),
    )

    assert response.status_code == 200
    clean_db.expire_all()
    release = clean_db.query(Release).one()
    assert release.edition.label == "Stephen Fry"
    assert release.edition.hardcover_edition_id == 30920116
    assert release.edition.narrator == "Stephen Fry"
    assert release.edition.download_state == DownloadState.GRABBED
    # the existing edition is untouched
    assert existing.download_state == DownloadState.IMPORTED
    assert existing.library_path is not None


@respx.mock
def test_grab_add_edition_requires_label(client, clean_db, imported_book):
    response = client.post(
        f"/books/{imported_book.id}/grab", data=grab_data(add_edition="1")
    )
    assert response.status_code == 409
    assert clean_db.query(Release).count() == 0


@respx.mock
def test_grab_add_edition_blocked_when_that_edition_is_busy(client, clean_db, imported_book):
    make_edition(clean_db, imported_book, label="Stephen Fry",
                 download_state=DownloadState.DOWNLOADING)

    response = client.post(
        f"/books/{imported_book.id}/grab",
        data=grab_data(add_edition="1", edition_label="Stephen Fry"),
    )

    assert response.status_code == 409
    assert "already downloading" in response.json()["detail"]


@respx.mock
def test_first_grab_with_edition_choice_creates_labelled_edition(client, clean_db, book):
    mock_details()
    mock_deluge()

    response = client.post(
        f"/books/{book.id}/grab",
        data=grab_data(
            hc_edition="30920116",
            hclabel_30920116="Stephen Fry",
            hcnarr_30920116="Stephen Fry",
        ),
    )

    assert response.status_code == 200
    release = clean_db.query(Release).one()
    assert release.edition.label == "Stephen Fry"
    assert release.edition.hardcover_edition_id == 30920116


@respx.mock
def test_plain_release_picker_offers_edition_options(client, book):
    mock_search()

    response = client.get(f"/books/{book.id}/releases")

    assert response.status_code == 200
    assert f"/books/{book.id}/editions/options" in response.text


@respx.mock
def test_edition_options_fragment_lists_hardcover_editions(client, clean_db, book):
    make_edition(clean_db, book, label="Jim Dale")  # a series/book label to suggest
    mock_editions([edition_row(30920116)])

    response = client.get(f"/books/{book.id}/editions/options")

    assert response.status_code == 200
    assert 'value="30920116"' in response.text
    assert "Stephen Fry" in response.text
    assert "Jim Dale" in response.text  # suggestion datalist


# --- per-edition replace ----------------------------------------------------


@respx.mock
def test_replace_one_edition_leaves_the_other_intact(
    client, clean_db, imported_book, test_settings
):
    mock_details()
    mock_deluge()
    mock_search()
    first = imported_book.editions[0]
    first.label = "Jim Dale"
    other_dir = test_settings.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown {Fry}"
    other_dir.mkdir(parents=True)
    (other_dir / "fry.mp3").write_bytes(b"audio")
    other = make_edition(clean_db, imported_book, label="Fry",
                         download_state=DownloadState.IMPORTED,
                         library_path=str(other_dir))

    picker = client.get(f"/books/{imported_book.id}/releases?replace=1&edition_id={other.id}")
    assert f'name="edition_id" value="{other.id}"' in picker.text

    response = client.post(
        f"/books/{imported_book.id}/grab",
        data=grab_data(replace="1", edition_id=str(other.id), remove="immediately"),
    )

    assert response.status_code == 200
    clean_db.expire_all()
    # the replaced edition lost its files; the sibling kept everything
    assert other.library_path is None
    assert other.download_state == DownloadState.GRABBED
    assert not other_dir.exists()
    assert first.library_path is not None
    assert first.download_state == DownloadState.IMPORTED
    release = clean_db.query(Release).one()
    assert release.edition_id == other.id


# --- the labelled import lands in the labelled folder -----------------------


def test_import_lands_in_labelled_folder(clean_db, book, test_settings):
    import shutil

    from app.services.importer import import_release

    for d in (test_settings.download_dir, test_settings.library_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    edition = make_edition(clean_db, book, label="Stephen Fry",
                           download_state=DownloadState.GRABBED)
    release = Release(edition=edition, guid="g", indexer="ABB",
                      title="The Mayor of Noobtown [M4B]", status="grabbed")
    clean_db.add(release)
    clean_db.commit()
    source = test_settings.download_dir / "The Mayor of Noobtown [M4B]"
    source.mkdir()
    (source / "book.mp3").write_bytes(b"audio")

    assert import_release(clean_db, release, source) is True

    dest = test_settings.library_dir / "Ryan Rimmel" / "The Mayor of Noobtown {Stephen Fry}"
    assert (dest / "book.mp3").exists()
    assert edition.library_path == str(dest)
