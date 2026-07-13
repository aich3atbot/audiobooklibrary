"""Catalogue payloads for the ABS API (libraries, items, progress).
Filled in by the catalogue/playback phases."""

from typing import Any

from sqlalchemy.orm import Session


def all_media_progress(db: Session) -> list[dict[str, Any]]:
    return []
