import httpx
import pytest
import respx

from app.clients.download_client import DownloadClientError
from app.clients.qbittorrent import QBittorrentClient

QB = "http://qbittorrent.test:8113"
API = f"{QB}/api/v2"
MAGNET = "magnet:?xt=urn:btih:AD5FAE5FFDA056F9F45131045D140326BBAFC4DC&dn=Book"
HASH = "ad5fae5ffda056f9f45131045d140326bbafc4dc"


def login_ok():
    # qBittorrent 5.x answers 204 on login (docs say 200 "Ok.").
    return respx.post(f"{API}/auth/login").respond(204)


def added(*hashes):
    return httpx.Response(
        200, json={"added_torrent_ids": list(hashes), "failure_count": 0, "success_count": 1}
    )


@pytest.fixture
def qbit():
    with QBittorrentClient(QB, username="admin", password="pw") as client:
        yield client


@pytest.fixture
def labeled_qbit():
    with QBittorrentClient(QB, username="admin", password="pw", label="abl") as client:
        yield client


@respx.mock
def test_login_sends_credentials_and_check_reports_version(qbit):
    login = login_ok()
    respx.get(f"{API}/app/version").respond(200, text="v5.2.3")

    assert qbit.check() == "qBittorrent v5.2.3"
    assert "username=admin" in login.calls[0].request.content.decode()


@respx.mock
def test_bad_credentials_raise(qbit):
    respx.post(f"{API}/auth/login").respond(401, text="Unauthorized")

    with pytest.raises(DownloadClientError, match="login failed"):
        qbit.check()


@respx.mock
def test_old_style_login_failure_raises(qbit):
    # Pre-5.x rejects credentials with 200 "Fails."
    respx.post(f"{API}/auth/login").respond(200, text="Fails.")

    with pytest.raises(DownloadClientError, match="login failed"):
        qbit.check()


@respx.mock
def test_expired_session_logs_in_again_and_retries(qbit):
    login_ok()
    respx.get(f"{API}/app/version").mock(
        side_effect=[httpx.Response(403, text="Forbidden"), httpx.Response(200, text="v5.2.3")]
    )

    assert qbit.check() == "qBittorrent v5.2.3"


@respx.mock
def test_add_magnet_returns_hash_from_response(qbit):
    login_ok()
    add = respx.post(f"{API}/torrents/add").mock(return_value=added(HASH))

    assert qbit.add_magnet(MAGNET) == HASH

    body = add.calls[0].request.content.decode()
    assert "category" not in body  # no label configured


@respx.mock
def test_add_magnet_sets_category_when_labeled(labeled_qbit):
    login_ok()
    add = respx.post(f"{API}/torrents/add").mock(return_value=added(HASH))

    labeled_qbit.add_magnet(MAGNET)

    assert "category=abl" in add.calls[0].request.content.decode()


@respx.mock
def test_add_magnet_falls_back_to_magnet_hash_on_bare_ok(qbit):
    # Pre-5.x answers a bare "Ok." with no hashes.
    login_ok()
    respx.post(f"{API}/torrents/add").respond(200, text="Ok.")

    assert qbit.add_magnet(MAGNET) == HASH  # parsed from the magnet, lowercased


@respx.mock
def test_add_magnet_treats_duplicate_as_success(qbit):
    login_ok()
    respx.post(f"{API}/torrents/add").respond(409, text="Conflict")

    assert qbit.add_magnet(MAGNET) == HASH


@respx.mock
def test_add_magnet_propagates_real_errors(qbit):
    login_ok()
    respx.post(f"{API}/torrents/add").respond(415, text="Unsupported media type")

    with pytest.raises(DownloadClientError, match="415"):
        qbit.add_magnet(MAGNET)


@respx.mock
def test_add_magnet_rejected_with_old_style_fails(qbit):
    login_ok()
    respx.post(f"{API}/torrents/add").respond(200, text="Fails.")

    with pytest.raises(DownloadClientError, match="rejected"):
        qbit.add_magnet(MAGNET)


@respx.mock
def test_get_status_maps_torrents(qbit):
    login_ok()
    route = respx.get(f"{API}/torrents/info").respond(
        200,
        json=[
            {
                "hash": HASH.upper(),
                "name": "Project Hail Mary",
                "progress": 0.425,
                "state": "downloading",
                "amount_left": 500000000,
                "save_path": "/downloads",
                "total_size": 881000000,
            }
        ],
    )

    status = qbit.get_status([HASH.upper()])[HASH]  # keyed by lowercase hash

    assert status.name == "Project Hail Mary"  # no content_path yet → display name
    assert status.progress == 42.5  # qBittorrent's 0..1 scaled to 0..100
    assert status.state == "downloading"
    assert status.is_finished is False
    assert status.total_size == 881000000
    assert route.calls[0].request.url.params["hashes"] == HASH


@respx.mock
def test_name_comes_from_root_path_not_display_name(qbit):
    # A magnet's dn sticks as the display name even after metadata arrives, so
    # ``name`` may not match the folder on disk — root_path's basename does.
    login_ok()
    respx.get(f"{API}/torrents/info").respond(
        200,
        json=[
            {"hash": HASH, "name": "Chamber of Secrets (Full Cast) - J.K. Rowling",
             "root_path": "/downloads/Chamber of Secrets (Full-Cast Edition) MP3",
             "content_path": "/downloads/Chamber of Secrets (Full-Cast Edition) MP3",
             "progress": 1.0, "state": "stoppedUP", "save_path": "/downloads",
             "total_size": 100},
        ],
    )

    status = qbit.get_status([HASH])[HASH]

    assert status.name == "Chamber of Secrets (Full-Cast Edition) MP3"


@respx.mock
def test_single_file_torrent_reports_its_folder_not_the_file(qbit):
    # A torrent holding exactly one file gets a content_path pointing at the
    # *file*, even though the file sits inside a root folder — and it is the
    # folder that appears in the download directory. root_path has it right.
    login_ok()
    respx.get(f"{API}/torrents/info").respond(
        200,
        json=[
            {"hash": HASH, "name": "Threads of Fate (Ascend Online Book 5) - Luke Chmilenko",
             "root_path": "/downloads/Luke Chmilenko - Ascend Online Book 5 - Threads of Fate",
             "content_path": "/downloads/Luke Chmilenko - Ascend Online Book 5 - Threads of"
                             " Fate/Threads of Fate꞉ Ascend Online, Book 5.m4b",
             "progress": 1.0, "state": "stoppedUP", "save_path": "/downloads",
             "total_size": 1128856418},
        ],
    )

    status = qbit.get_status([HASH])[HASH]

    assert status.name == "Luke Chmilenko - Ascend Online Book 5 - Threads of Fate"


@respx.mock
def test_torrent_without_a_root_folder_falls_back_to_content_path(qbit):
    # root_path is empty when the torrent has no root folder; then the lone
    # file *is* the entry in the download directory.
    login_ok()
    respx.get(f"{API}/torrents/info").respond(
        200,
        json=[
            {"hash": HASH, "name": "Project Hail Mary - Andy Weir",
             "root_path": "",
             "content_path": "/downloads/Project Hail Mary.m4b",
             "progress": 1.0, "state": "stoppedUP", "save_path": "/downloads",
             "total_size": 100},
        ],
    )

    status = qbit.get_status([HASH])[HASH]

    assert status.name == "Project Hail Mary.m4b"


@respx.mock
def test_finished_torrent_keys_off_progress_not_amount_left(qbit):
    # A metadata-less torrent reports amount_left 0 and total_size -1 while
    # 0% done — progress is the only trustworthy completion signal.
    login_ok()
    respx.get(f"{API}/torrents/info").respond(
        200,
        json=[
            {"hash": HASH, "name": "no metadata yet", "progress": 0, "amount_left": 0,
             "state": "stoppedDL", "save_path": "/downloads", "total_size": -1},
            {"hash": "b" * 40, "name": "Done", "progress": 1.0, "amount_left": 0,
             "state": "stalledUP", "save_path": "/downloads", "total_size": 100},
        ],
    )

    statuses = qbit.get_status([HASH, "b" * 40])

    assert statuses[HASH].is_finished is False
    assert statuses[HASH].total_size == 0  # -1 clamped
    assert statuses["b" * 40].is_finished is True


@respx.mock
def test_get_status_without_hashes_makes_no_call(qbit):
    route = respx.get(f"{API}/torrents/info")

    assert qbit.get_status([]) == {}
    assert not route.called


@respx.mock
def test_remove_torrent_deletes_the_data_too(qbit):
    login_ok()
    route = respx.post(f"{API}/torrents/delete").respond(200)

    qbit.remove_torrent(HASH.upper())

    body = route.calls[0].request.content.decode()
    assert f"hashes={HASH}" in body  # lowercased
    assert "deleteFiles=true" in body
