"""Deluge client, over the Web UI's JSON-RPC endpoint (``POST /json``).

Verified against Deluge WebUI 2.2.0:
- Auth is ``auth.login([password])`` and password-only — there is no username
  (DOWNLOAD_USERNAME is accepted in config but unused, reserved for future
  clients). An empty password is valid.
- Login sets a session cookie, which ``httpx.Client`` persists for us. Sessions
  expire; an expired one answers "Not authenticated" (error code 1), so calls
  re-login once and retry.
- The web process talks to the daemon separately: if ``web.connected()`` is
  false we ``web.connect()`` to the first host from ``web.get_hosts()``.
- ``daemon.info`` does *not* exist on 2.2.0 ("Unknown method"); the version
  comes from ``daemon.get_version``.
- A finished torrent reports ``is_finished: true`` but its ``state`` may be
  anything ("Queued", "Seeding", ...) — never key completion off the state.
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

STATUS_KEYS = ["name", "progress", "state", "is_finished", "save_path", "total_size"]
NOT_AUTHENTICATED = 1


class DelugeClient:
    def __init__(
        self, base_url: str, password: str = "", timeout: float = 30.0, label: str = ""
    ):
        self._url = f"{base_url.rstrip('/')}/json"
        self._password = password
        # Deluge's Label plugin only accepts lowercase names.
        self._label = label.strip().lower()
        self._client = httpx.Client(timeout=timeout)
        self._request_id = 0
        self._connected = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DelugeClient":
        return self
    def __exit__(self, *exc) -> None:
        self.close()

    # -- JSON-RPC plumbing -------------------------------------------------

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        response = self._client.post(
            self._url, json={"method": method, "params": params, "id": self._request_id}
        )
        response.raise_for_status()
        payload = response.json()
        if error := payload.get("error"):
            failure = DownloadClientError(f"Deluge {method} failed: {error.get('message')}")
            failure.code = error.get("code")
            raise failure
        return payload.get("result")

    def _call(self, method: str, params: list[Any]) -> Any:
        """An authenticated call: connects first, and re-logs-in once if the
        session has expired underneath us."""
        self._ensure_connected()
        try:
            return self._rpc(method, params)
        except DownloadClientError as exc:
            if getattr(exc, "code", None) != NOT_AUTHENTICATED:
                raise
            logger.info("Deluge session expired, logging in again")
            self._connected = False
            self._ensure_connected()
            return self._rpc(method, params)

    def _ensure_connected(self) -> None:
        if self._connected:
            return
        if self._rpc("auth.login", [self._password]) is not True:
            raise DownloadClientError("Deluge login failed — check DOWNLOAD_PASSWORD")
        if not self._rpc("web.connected", []):
            hosts = self._rpc("web.get_hosts", []) or []
            if not hosts:
                raise DownloadClientError("Deluge web UI has no daemon host configured")
            self._rpc("web.connect", [hosts[0][0]])
        self._connected = True

    # -- DownloadClient protocol -------------------------------------------

    def add_magnet(self, magnet_uri: str) -> str:
        try:
            result = self._call("core.add_torrent_magnet", [magnet_uri, {}])
        except DownloadClientError as exc:
            # Re-grabbing a torrent Deluge already has is a success, not an error.
            if "already" not in str(exc).lower():
                raise
            logger.info("Deluge already has this torrent; reusing it")
            result = None
        info_hash = (result or hash_from_magnet(magnet_uri)).lower()
        self._apply_label(info_hash)
        return info_hash

    def _apply_label(self, info_hash: str) -> None:
        """Best-effort: labeling needs the Label plugin, and a torrent without
        its label is still a perfectly good download."""
        if not self._label:
            return
        try:
            if self._label not in (self._call("label.get_labels", []) or []):
                self._call("label.add", [self._label])
            self._call("label.set_torrent", [info_hash, self._label])
        except DownloadClientError as exc:
            logger.warning("Could not label torrent %s as %r: %s", info_hash, self._label, exc)

    def get_status(self, info_hashes: Sequence[str]) -> dict[str, TorrentStatus]:
        if not info_hashes:
            return {}
        filter_ = {"id": [h.lower() for h in info_hashes]}
        result = self._call("core.get_torrents_status", [filter_, STATUS_KEYS]) or {}
        return {
            info_hash.lower(): TorrentStatus(
                info_hash=info_hash.lower(),
                name=status.get("name") or "",
                progress=float(status.get("progress") or 0.0),
                state=status.get("state") or "",
                is_finished=bool(status.get("is_finished")),
                save_path=status.get("save_path") or "",
                total_size=int(status.get("total_size") or 0),
            )
            for info_hash, status in result.items()
        }

    def remove_torrent(self, info_hash: str, remove_data: bool = True) -> None:
        try:
            self._call("core.remove_torrent", [info_hash.lower(), remove_data])
        except DownloadClientError as exc:
            # Deluge raises InvalidTorrentError for a hash it doesn't have.
            # We wanted it gone; it is gone.
            if "invalid" not in str(exc).lower() and "not in session" not in str(exc).lower():
                raise
            logger.info("Deluge no longer has torrent %s", info_hash)

    def check(self) -> str:
        version = self._call("daemon.get_version", [])
        return f"Deluge daemon {version}"
