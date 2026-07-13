"""JSON payload builders matching the ABS "old model" shapes clients consume.
Shapes are pinned in docs/abs-api-contract.md — change them only against source."""

import time
from typing import Any

from sqlalchemy.orm import Session

from app.abs import tokens
from app.config import get_settings

SERVER_VERSION = "2.35.1"  # ABS version we emulate (feature-gating in apps)
SOURCE = "docker"
LIBRARY_ID = "lib_audiobooks"
FOLDER_ID = "fol_audiobooks"
USER_CREATED_AT_MS = 1704067200000  # fixed epoch; apps only display this


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


def user_json(db: Session, minimal: bool = False) -> dict[str, Any]:
    """ABS user.toOldJSONForBrowser equivalent for our single user."""
    settings = get_settings()
    json: dict[str, Any] = {
        "id": tokens.user_id(),
        "username": settings.auth_username,
        "email": None,
        "type": "root",
        "token": tokens.create_legacy_token(),
        "isOldToken": False,
        "mediaProgress": media_progress_list(db),
        "seriesHideFromContinueListening": [],
        "bookmarks": _bookmarks_list(db),
        "isActive": True,
        "isLocked": False,
        "lastSeen": now_ms(),
        "createdAt": USER_CREATED_AT_MS,
        "permissions": permissions(),
        "librariesAccessible": [],
        "itemTagsSelected": [],
        "hasOpenIDLink": False,
    }
    if minimal:
        del json["mediaProgress"]
        del json["bookmarks"]
    return json


def media_progress_list(db: Session) -> list[dict[str, Any]]:
    from app.abs.catalogue import all_media_progress  # late import: avoids cycle

    return all_media_progress(db)


def _bookmarks_list(db: Session) -> list[dict[str, Any]]:
    from app.abs.catalogue import all_bookmarks  # late import: avoids cycle

    return all_bookmarks(db)


def login_payload(db: Session, access_token: str, refresh_token: str | None) -> dict[str, Any]:
    user = user_json(db)
    user["accessToken"] = access_token
    user["refreshToken"] = refresh_token
    return {
        "user": user,
        "userDefaultLibraryId": LIBRARY_ID,
        "serverSettings": server_settings(),
        "ereaderDevices": [],
        "Source": SOURCE,
    }
