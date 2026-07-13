"""Prowlarr REST API client.

Searches use the audiobook category (3030 by default). A "grab" is a POST
back to the search endpoint with the release's guid + indexerId; Prowlarr
resolves it from its recent-search cache and forwards it to the configured
download client — so a grab must follow shortly after the search that
surfaced the release (our UI flow guarantees this).
"""

from collections.abc import Sequence
from typing import Any

import httpx


class ProwlarrError(RuntimeError):
    pass


class ProwlarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        # generous timeout: Prowlarr fans out to indexers, which can be slow
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ProwlarrClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        response = self._client.get("/api/v1/system/status")
        response.raise_for_status()
        return response.json()

    def search(self, query: str, categories: Sequence[int] = (3030,)) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("query", query), ("type", "search")]
        params.extend(("categories", c) for c in categories)
        response = self._client.get("/api/v1/search", params=params)
        response.raise_for_status()
        return response.json()

    def grab(self, guid: str, indexer_id: int) -> None:
        response = self._client.post(
            "/api/v1/search", json={"guid": guid, "indexerId": indexer_id}
        )
        if response.status_code >= 400:
            raise ProwlarrError(
                f"grab failed ({response.status_code}): {response.text[:300]}"
            )
