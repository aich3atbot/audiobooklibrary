"""qBittorrent client, over the Web UI API (``/api/v2``).

Verified against qBittorrent 5.2.3 / Web API 2.15.1:
- Login answers **204** (the docs say 200 "Ok."); bad credentials answer 401.
  The session is the SID cookie, which ``httpx.Client`` persists for us.
  Expired/unauthenticated calls answer 403, so calls re-login once and retry.
- ``torrents/add`` answers JSON with ``added_torrent_ids`` (older versions
  answer a bare "Ok."), and re-adding a torrent it already has answers 409.
- Passing ``category=`` on add **auto-creates the category** — no
  ``createCategory`` call is needed.
- ``torrents/info``: ``progress`` is 0..1. ``amount_left`` is 0 for a torrent
  that has no metadata yet, so it is *not* a completion signal —
  ``progress >= 1.0`` is. ``total_size`` is -1 before metadata arrives.
- ``torrents/delete`` answers 200 even for hashes it doesn't have.
- ``name`` is the *display* name: a magnet's ``dn`` sticks even after metadata
  arrives, so it can differ from the folder on disk. ``content_path`` holds the
  real content location; we report its basename as the torrent's name.
"""

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from app.clients.download_client import (
    DownloadClientError,
    TorrentStatus,
    hash_from_magnet,
)

logger = logging.getLogger(__name__)


class QBittorrentClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 30.0,
        label: str = "",
    ):
        self._base = f"{base_url.rstrip('/')}/api/v2"
        self._username = username
        self._password = password
        self._label = label.strip()  # applied as a qBittorrent category
        self._client = httpx.Client(timeout=timeout)
        self._connected = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QBittorrentClient":
        return self
    def __exit__(self, *exc) -> None:
        self.close()

    # -- HTTP plumbing -------------------------------------------------------

    def _login(self) -> None:
        response = self._client.post(
            f"{self._base}/auth/login",
            data={"username": self._username, "password": self._password},
        )
        # Older versions answer 200 "Fails." for bad credentials, 5.x answers 401.
        if response.is_error or response.text.strip() == "Fails.":
            raise DownloadClientError(
                "qBittorrent login failed — check DOWNLOAD_USERNAME/DOWNLOAD_PASSWORD"
            )
        self._connected = True

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """An authenticated call: logs in first, and re-logs-in once if the
        session has expired underneath us (qBittorrent answers 403)."""
        if not self._connected:
            self._login()
        response = self._client.request(method, f"{self._base}/{path}", **kwargs)
        if response.status_code == 403:
            logger.info("qBittorrent session expired, logging in again")
            self._connected = False
            self._login()
            response = self._client.request(method, f"{self._base}/{path}", **kwargs)
        if response.is_error:
            failure = DownloadClientError(
                f"qBittorrent {path} failed: HTTP {response.status_code} {response.text.strip()}"
            )
            failure.code = response.status_code
            raise failure
        return response

    # -- DownloadClient protocol ----------------------------------------------

    def add_magnet(self, magnet_uri: str) -> str:
        data = {"urls": magnet_uri}
        if self._label:
            data["category"] = self._label
        try:
            response = self._call("POST", "torrents/add", data=data)
        except DownloadClientError as exc:
            # Re-grabbing a torrent qBittorrent already has is a success, not an error.
            if exc.code != 409:
                raise
            logger.info("qBittorrent already has this torrent; reusing it")
            return hash_from_magnet(magnet_uri)
        if response.text.strip() == "Fails.":  # older versions reject with 200 "Fails."
            raise DownloadClientError("qBittorrent rejected the magnet")
        try:
            added = response.json().get("added_torrent_ids") or []
        except ValueError:  # pre-5.x answers a bare "Ok."
            added = []
        return (added[0] if added else hash_from_magnet(magnet_uri)).lower()

    def get_status(self, info_hashes: Sequence[str]) -> dict[str, TorrentStatus]:
        if not info_hashes:
            return {}
        hashes = "|".join(h.lower() for h in info_hashes)
        response = self._call("GET", "torrents/info", params={"hashes": hashes})
        statuses = {}
        for torrent in response.json():
            info_hash = torrent["hash"].lower()
            progress = float(torrent.get("progress") or 0.0)
            # ``name`` is the display name — for a magnet add it stays the
            # magnet's dn and never updates to the metadata name, so it may not
            # match anything on disk. ``content_path`` points at the actual
            # root folder/file; its basename is the on-disk name.
            content_path = (torrent.get("content_path") or "").replace("\\", "/")
            statuses[info_hash] = TorrentStatus(
                info_hash=info_hash,
                name=content_path.rstrip("/").rsplit("/", 1)[-1]
                or torrent.get("name")
                or "",
                progress=progress * 100,
                state=torrent.get("state") or "",
                # amount_left is 0 before metadata arrives, so it can't be the
                # signal; qBittorrent reports progress 1.0 when done.
                is_finished=progress >= 1.0,
                save_path=torrent.get("save_path") or "",
                total_size=max(int(torrent.get("total_size") or 0), 0),
            )
        return statuses

    def remove_torrent(self, info_hash: str, remove_data: bool = True) -> None:
        # Answers 200 even for a hash it doesn't have — already the goal state.
        self._call(
            "POST",
            "torrents/delete",
            data={"hashes": info_hash.lower(), "deleteFiles": str(remove_data).lower()},
        )

    def check(self) -> str:
        version = self._call("GET", "app/version").text.strip()
        return f"qBittorrent {version}"
