import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import get_current_user
from app.clients.audiobookbay import AudioBookBayClient
from app.clients.download_client import get_download_client
from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.models import User
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

CHECK_TIMEOUT = 10.0


def check_hardcover(user: User) -> tuple[bool, str]:
    if not user.hardcover_token:
        return False, "no Hardcover token set for your account (ask the admin)"
    try:
        with HardcoverClient(user.hardcover_token, timeout=CHECK_TIMEOUT) as client:
            account = client.me()
        return True, f"connected as {account['username']}"
    except Exception as exc:
        logger.warning("Hardcover connection check failed: %s", exc)
        return False, str(exc)


def check_indexer() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.index_url:
        return False, "INDEX_URL not set"
    try:
        with AudioBookBayClient(settings.index_url, timeout=CHECK_TIMEOUT) as client:
            return True, client.check()
    except Exception as exc:
        logger.warning("Indexer connection check failed: %s", exc)
        return False, str(exc)


def check_download_client() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.download_client:
        return False, "DOWNLOAD_CLIENT not set — downloading disabled"
    # Deluge authenticates on the password alone, and an empty one is valid, so
    # the URL is all we can require here.
    if not settings.download_url:
        return False, "DOWNLOAD_URL not set"
    try:
        with get_download_client(timeout=CHECK_TIMEOUT) as client:
            return True, client.check()
    except Exception as exc:
        logger.warning("Download client connection check failed: %s", exc)
        return False, str(exc)


def check_ffmpeg() -> tuple[bool, str]:
    """Whether MP3 → M4B conversion is possible. Worth surfacing here because
    its absence shows up in the UI only as a missing button."""
    import subprocess

    settings = get_settings()
    try:
        result = subprocess.run(
            [settings.ffmpeg_path, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{settings.ffmpeg_path}: {exc}"
    if result.returncode != 0:
        return False, f"{settings.ffmpeg_path} exited {result.returncode}"
    return True, result.stdout.splitlines()[0] if result.stdout else "available"


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(get_current_user)):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "hardcover": check_hardcover(user),
            "indexer": check_indexer(),
            "download_client": check_download_client(),
            "ffmpeg": check_ffmpeg(),
            "settings": settings,
            "last_sync": user.last_sync_at,
            "last_sync_result": user.last_sync_result,
        },
    )
