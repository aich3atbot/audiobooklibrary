import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import get_db
from app.models import (
    TRANSCODE_ACTIVE,
    Book,
    DownloadState,
    Edition,
    Release,
    TranscodeJob,
    TranscodeState,
)
from app.services.downloads import drop_from_client
from app.services.importer import ACTIVE_STATUSES, import_release, replace_key
from app.services.sync import delete_state
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# How often the page refreshes itself, decided per render: the fast rate while
# there is something to watch, the slow one when there is not. A tab left open
# on an idle library should not poll like one watching a download.
ACTIVE_POLL_SECONDS = 5
IDLE_POLL_SECONDS = 30


def _get_release(db: Session, release_id: int) -> Release:
    release = db.get(Release, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="release not found")
    return release


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, db: Session = Depends(get_db)):
    def releases_with(*statuses):
        return (
            db.scalars(
                select(Release)
                .where(Release.status.in_(statuses))
                .options(
                    joinedload(Release.edition).joinedload(Edition.book).joinedload(Book.author),
                    joinedload(Release.user),
                )
                .order_by(Release.grabbed_at.desc())
            )
            .unique()
            .all()
        )

    imported = (
        db.scalars(
            select(Edition)
            .where(Edition.download_state == DownloadState.IMPORTED)
            .options(joinedload(Edition.book).joinedload(Book.author))
            .order_by(Edition.updated_at.desc())
            .limit(20)
        )
        .unique()
        .all()
    )
    def transcodes(*states):
        return (
            db.scalars(
                select(TranscodeJob)
                .where(TranscodeJob.state.in_(states))
                .options(
                    joinedload(TranscodeJob.edition)
                    .joinedload(Edition.book)
                    .joinedload(Book.author),
                    joinedload(TranscodeJob.user),
                )
                .order_by(TranscodeJob.id.desc())
            )
            .unique()
            .all()
        )

    active = releases_with(*ACTIVE_STATUSES)
    transcoding = transcodes(*TRANSCODE_ACTIVE)
    return templates.TemplateResponse(
        request,
        # htmx asks for the fragment; a browser navigation gets the whole page.
        "_activity_content.html" if request.headers.get("hx-request") else "activity.html",
        {
            "active": active,
            "failed": releases_with("failed"),
            "imported": imported,
            "transcoding": transcoding,
            "transcode_failed": transcodes(TranscodeState.FAILED),
            "poll_seconds": (
                ACTIVE_POLL_SECONDS if (active or transcoding) else IDLE_POLL_SECONDS
            ),
        },
    )


@router.post("/releases/{release_id}/cancel")
def cancel_release(release_id: int, db: Session = Depends(get_db)):
    """Cancel a release: remove its torrent and downloaded data from the
    download client, and stop tracking it here. This deletes the data and ends
    any seeding of it — that is the point of cancelling."""
    release = _get_release(db, release_id)
    try:
        drop_from_client(release)
        release.error = None
    except Exception as exc:
        # Cancel locally anyway: a download client we can't reach must not
        # trap the release here forever. But say so — the torrent is probably
        # still running, and only the user can go remove it.
        logger.exception("Could not remove torrent for %s", release.title)
        release.error = (
            f"Cancelled here, but the torrent could not be removed from the download "
            f"client ({exc}). It may still be downloading or seeding — remove it there."
        )

    release.status = "cancelled"
    other_active = db.scalar(
        select(Release.id).where(
            Release.edition_id == release.edition_id,
            Release.id != release.id,
            Release.status.in_(ACTIVE_STATUSES),
        )
    )
    delete_state(db, replace_key(release))
    if other_active is None:
        # A cancelled replace whose old files survived stays available.
        release.edition.download_state = (
            DownloadState.IMPORTED if release.edition.library_path else DownloadState.NONE
        )
    db.commit()
    return RedirectResponse(url="/activity", status_code=303)


@router.post("/releases/{release_id}/retry")
def retry_release(release_id: int, db: Session = Depends(get_db)):
    release = _get_release(db, release_id)
    if release.status != "failed":
        raise HTTPException(status_code=409, detail="release is not failed")
    release.status = "grabbed"
    release.error = None
    release.edition.download_state = DownloadState.GRABBED
    db.commit()
    return RedirectResponse(url="/activity", status_code=303)


@router.post("/releases/{release_id}/manual-import")
def manual_import(release_id: int, folder: str = Form(...), db: Session = Depends(get_db)):
    """Import a specific entry from the download directory for this release,
    for when name matching can't find the download on its own."""
    release = _get_release(db, release_id)
    download_dir = get_settings().download_dir.resolve()
    source = (download_dir / folder.strip()).resolve()
    if not source.is_relative_to(download_dir) or source == download_dir:
        raise HTTPException(status_code=400, detail="folder must be inside the download directory")
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"no such entry: {folder.strip()}")
    import_release(db, release, source)
    return RedirectResponse(url="/activity", status_code=303)
