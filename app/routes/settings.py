import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.clients.hardcover import HardcoverClient
from app.clients.prowlarr import ProwlarrClient
from app.config import get_settings
from app.db import get_db
from app.services.sync import LAST_SYNC_KEY, LAST_SYNC_RESULT_KEY, get_state
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

CHECK_TIMEOUT = 10.0


def check_hardcover() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.hardcover_token:
        return False, "HARDCOVER_TOKEN not set"
    try:
        with HardcoverClient(settings.hardcover_token, timeout=CHECK_TIMEOUT) as client:
            user = client.me()
        return True, f"connected as {user['username']}"
    except Exception as exc:
        logger.warning("Hardcover connection check failed: %s", exc)
        return False, str(exc)


def check_prowlarr() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.prowlarr_api_key:
        return False, "PROWLARR_API_KEY not set"
    try:
        with ProwlarrClient(
            settings.prowlarr_url, settings.prowlarr_api_key, timeout=CHECK_TIMEOUT
        ) as client:
            status = client.status()
        return True, f"{status.get('appName', 'Prowlarr')} {status.get('version', '?')}"
    except Exception as exc:
        logger.warning("Prowlarr connection check failed: %s", exc)
        return False, str(exc)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "hardcover": check_hardcover(),
            "prowlarr": check_prowlarr(),
            "settings": settings,
            "last_sync": get_state(db, LAST_SYNC_KEY),
            "last_sync_result": get_state(db, LAST_SYNC_RESULT_KEY),
        },
    )
