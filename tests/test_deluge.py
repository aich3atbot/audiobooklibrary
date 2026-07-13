import json

import httpx
import pytest
import respx

from app.clients.deluge import DelugeClient
from app.clients.download_client import DownloadClientError, get_download_client

DELUGE = "http://deluge.test:8112"
RPC = f"{DELUGE}/json"
MAGNET = "magnet:?xt=urn:btih:AD5FAE5FFDA056F9F45131045D140326BBAFC4DC&dn=Book"
HASH = "ad5fae5ffda056f9f45131045d140326bbafc4dc"


def ok(result):
    return {"result": result, "error": None}


def rpc_error(message, code=2):
    return {"result": None, "error": {"message": message, "code": code}}


def responder(handlers: dict[str, list | object]):
    """Answer each JSON-RPC call by method name. A list value is consumed one
    response per call, so a method can answer differently across calls."""

    def handle(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        payload = handlers[method]
        if isinstance(payload, list):
            payload = payload.pop(0)
        return httpx.Response(200, json=payload)

    return handle


@pytest.fixture
def deluge():
    with DelugeClient(DELUGE, password="") as client:
        yield client


@respx.mock
def test_check_logs_in_connects_and_reports_version(deluge):
    route = respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(False),
                "web.get_hosts": ok([["host-id-1", "127.0.0.1", 58846, "localclient"]]),
                "web.connect": ok(None),
                "daemon.get_version": ok("2.2.0"),
            }
        )
    )

    assert deluge.check() == "Deluge daemon 2.2.0"

    calls = [json.loads(c.request.content) for c in route.calls]
    assert [c["method"] for c in calls] == [
        "auth.login",
        "web.connected",
        "web.get_hosts",
        "web.connect",
        "daemon.get_version",
    ]
    assert calls[0]["params"] == [""]  # empty password is valid
    assert calls[3]["params"] == ["host-id-1"]


@respx.mock
def test_already_connected_web_ui_skips_connect(deluge):
    route = respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "daemon.get_version": ok("2.2.0"),
            }
        )
    )

    deluge.check()

    methods = [json.loads(c.request.content)["method"] for c in route.calls]
    assert "web.get_hosts" not in methods


@respx.mock
def test_bad_password_raises(deluge):
    respx.post(RPC).mock(
        side_effect=responder({"auth.login": ok(False)})  # Deluge answers false, not an error
    )

    with pytest.raises(DownloadClientError, match="login failed"):
        deluge.check()


@respx.mock
def test_add_magnet_returns_hash(deluge):
    route = respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.add_torrent_magnet": ok(HASH),
            }
        )
    )

    assert deluge.add_magnet(MAGNET) == HASH

    add = [json.loads(c.request.content) for c in route.calls][-1]
    assert add["params"] == [MAGNET, {}]


@respx.mock
def test_add_magnet_treats_duplicate_as_success(deluge):
    respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.add_torrent_magnet": rpc_error("Torrent already in session"),
            }
        )
    )

    # Hash comes from the magnet itself, lowercased.
    assert deluge.add_magnet(MAGNET) == HASH


@respx.mock
def test_add_magnet_propagates_real_errors(deluge):
    respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.add_torrent_magnet": rpc_error("Invalid magnet"),
            }
        )
    )

    with pytest.raises(DownloadClientError, match="Invalid magnet"):
        deluge.add_magnet(MAGNET)


@respx.mock
def test_get_status_maps_torrents(deluge):
    route = respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.get_torrents_status": ok(
                    {
                        HASH.upper(): {
                            "name": "Project Hail Mary",
                            "progress": 42.5,
                            "state": "Downloading",
                            "is_finished": False,
                            "save_path": "/downloads/complete",
                            "total_size": 881000000,
                        }
                    }
                ),
            }
        )
    )

    statuses = deluge.get_status([HASH])

    status = statuses[HASH]  # keyed by lowercase hash even though Deluge shouted it
    assert status.name == "Project Hail Mary"
    assert status.progress == 42.5
    assert status.state == "Downloading"
    assert status.is_finished is False
    assert status.total_size == 881000000

    params = json.loads(route.calls[-1].request.content)["params"]
    assert params[0] == {"id": [HASH]}
    assert "is_finished" in params[1]


@respx.mock
def test_remove_torrent_deletes_the_data_too(deluge):
    route = respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.remove_torrent": ok(True),
            }
        )
    )

    deluge.remove_torrent(HASH.upper())

    remove = json.loads(route.calls[-1].request.content)
    assert remove["method"] == "core.remove_torrent"
    assert remove["params"] == [HASH, True]  # lowercased hash, remove_data=True


@respx.mock
def test_remove_torrent_is_happy_if_deluge_already_lost_it(deluge):
    respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.remove_torrent": rpc_error("InvalidTorrentError: unknown torrent"),
            }
        )
    )

    deluge.remove_torrent(HASH)  # the goal state is "gone", and it is gone


@respx.mock
def test_remove_torrent_propagates_real_errors(deluge):
    respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.remove_torrent": rpc_error("Permission denied"),
            }
        )
    )

    with pytest.raises(DownloadClientError, match="Permission denied"):
        deluge.remove_torrent(HASH)


@respx.mock
def test_get_status_without_hashes_makes_no_call(deluge):
    route = respx.post(RPC)

    assert deluge.get_status([]) == {}
    assert not route.called


@respx.mock
def test_finished_torrent_is_reported_finished_whatever_its_state(deluge):
    respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": ok(True),
                "web.connected": ok(True),
                "core.get_torrents_status": ok(
                    {
                        HASH: {
                            "name": "Done",
                            "progress": 100.0,
                            "state": "Queued",  # a finished torrent can sit in any state
                            "is_finished": True,
                            "save_path": "/downloads/complete",
                            "total_size": 1,
                        }
                    }
                ),
            }
        )
    )

    assert deluge.get_status([HASH])[HASH].is_finished is True


@respx.mock
def test_expired_session_logs_in_again_and_retries(deluge):
    respx.post(RPC).mock(
        side_effect=responder(
            {
                "auth.login": [ok(True), ok(True)],
                "web.connected": [ok(True), ok(True)],
                "daemon.get_version": [
                    rpc_error("Not authenticated", code=1),
                    ok("2.2.0"),
                ],
            }
        )
    )

    assert deluge.check() == "Deluge daemon 2.2.0"


def test_get_download_client_dispatch(test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "download_url", DELUGE)
    monkeypatch.setattr(test_settings, "download_client", "deluge")
    with get_download_client() as client:
        assert isinstance(client, DelugeClient)

    monkeypatch.setattr(test_settings, "download_client", "transmission")
    with pytest.raises(DownloadClientError, match="unsupported DOWNLOAD_CLIENT"):
        get_download_client()

    monkeypatch.setattr(test_settings, "download_client", "deluge")
    monkeypatch.setattr(test_settings, "download_url", "")
    with pytest.raises(DownloadClientError, match="DOWNLOAD_URL is not set"):
        get_download_client()
