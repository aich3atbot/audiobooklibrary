"""JSON payload builders matching the ABS "old model" shapes clients consume.
Shapes are pinned in docs/abs-api-contract.md — change them only against source."""

import time
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from app.abs import tokens
from app.models import User

SERVER_VERSION = "2.35.1"  # ABS version we emulate (feature-gating in apps)
SOURCE = "docker"
LIBRARY_ID = "lib_audiobooks"
FOLDER_ID = "fol_audiobooks"
LIBRARY_CREATED_AT_MS = 1704067200000  # fixed epoch; apps only display this


def now_ms() -> int:
    return int(time.time() * 1000)


def item_id(book_id: int) -> str:
    return f"li_{book_id}"


def book_id_from_item(item_id_str: str) -> int | None:
    if not item_id_str.startswith("li_"):
        return None
    try:
        return int(item_id_str[3:])
    except ValueError:
        return None


def permissions() -> dict[str, Any]:
    return {
        "download": True,
        "update": True,
        "delete": True,
        "upload": True,
        "createEreader": True,
        "accessAllLibraries": True,
        "accessAllTags": True,
        "accessExplicitContent": True,
        "selectedTagsNotAccessible": False,
    }


def server_settings() -> dict[str, Any]:
    return {
        "id": "server-settings",
        "scannerParseSubtitle": False,
        "scannerFindCovers": False,
        "scannerCoverProvider": "google",
        "scannerPreferMatchedMetadata": False,
        "scannerDisableWatcher": True,
        "storeCoverWithItem": False,
        "storeMetadataWithItem": False,
        "metadataFileFormat": "json",
        "rateLimitLoginRequests": 10,
        "rateLimitLoginWindow": 600000,
        "allowIframe": False,
        "backupPath": "/config/backups",
        "backupSchedule": False,
        "backupsToKeep": 2,
        "maxBackupSize": 1,
        "loggerDailyLogsToKeep": 7,
        "loggerScannerLogsToKeep": 2,
        "homeBookshelfView": 1,
        "bookshelfView": 1,
        "podcastEpisodeSchedule": "0 * * * *",
        "sortingIgnorePrefix": False,
        "sortingPrefixes": ["the", "a"],
        "chromecastEnabled": False,
        "dateFormat": "MM/dd/yyyy",
        "timeFormat": "HH:mm",
        "language": "en-us",
        "logLevel": 2,
        "version": SERVER_VERSION,
        "buildNumber": 1,
        "authLoginCustomMessage": None,
        "authActiveAuthMethods": ["local"],
    }


def user_json(db: Session, user: User, minimal: bool = False) -> dict[str, Any]:
    """ABS user.toOldJSONForBrowser equivalent. Every account gets root
    permissions — apps only gate features on it (deliberate simplification)."""
    json: dict[str, Any] = {
        "id": user.uuid,
        "username": user.username,
        "email": None,
        "type": "root",
        "token": tokens.create_legacy_token(user),
        "isOldToken": False,
        "mediaProgress": media_progress_list(db, user),
        "seriesHideFromContinueListening": [],
        "bookmarks": _bookmarks_list(db, user),
        "isActive": True,
        "isLocked": False,
        "lastSeen": now_ms(),
        "createdAt": int(user.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000),
        "permissions": permissions(),
        "librariesAccessible": [],
        "itemTagsSelected": [],
        "hasOpenIDLink": False,
    }
    if minimal:
        del json["mediaProgress"]
        del json["bookmarks"]
    return json


def media_progress_list(db: Session, user: User) -> list[dict[str, Any]]:
    from app.abs.catalogue import all_media_progress  # late import: avoids cycle

    return all_media_progress(db, user)


def _bookmarks_list(db: Session, user: User) -> list[dict[str, Any]]:
    from app.abs.catalogue import all_bookmarks  # late import: avoids cycle

    return all_bookmarks(db, user)


def login_payload(
    db: Session, user: User, access_token: str, refresh_token: str | None
) -> dict[str, Any]:
    user_payload = user_json(db, user)
    user_payload["accessToken"] = access_token
    user_payload["refreshToken"] = refresh_token
    return {
        "user": user_payload,
        "userDefaultLibraryId": LIBRARY_ID,
        "serverSettings": server_settings(),
        "ereaderDevices": [],
        "Source": SOURCE,
    }
