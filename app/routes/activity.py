import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import get_db
from app.models import Book, DownloadState, Release
from app.services.importer import ACTIVE_STATUSES, import_release
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


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
                .options(joinedload(Release.book).joinedload(Book.author))
                .order_by(Release.grabbed_at.desc())
            )
            .unique()
            .all()
        )

    imported = (
        db.scalars(
            select(Book)
            .where(Book.download_state == DownloadState.IMPORTED)
            .options(joinedload(Book.author))
            .order_by(Book.updated_at.desc())
            .limit(20)
        )
        .unique()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "active": releases_with(*ACTIVE_STATUSES),
            "failed": releases_with("failed"),
            "imported": imported,
        },
    )


@router.post("/releases/{release_id}/cancel")
def cancel_release(release_id: int, db: Session = Depends(get_db)):
    release = _get_release(db, release_id)
    release.status = "cancelled"
    other_active = db.scalar(
        select(Release.id).where(
            Release.book_id == release.book_id,
            Release.id != release.id,
            Release.status.in_(ACTIVE_STATUSES),
        )
    )
    if other_active is None:
        release.book.download_state = DownloadState.NONE
    db.commit()
    return RedirectResponse(url="/activity", status_code=303)


@router.post("/releases/{release_id}/retry")
def retry_release(release_id: int, db: Session = Depends(get_db)):
    release = _get_release(db, release_id)
    if release.status != "failed":
        raise HTTPException(status_code=409, detail="release is not failed")
    release.status = "grabbed"
    release.error = None
    release.book.download_state = DownloadState.GRABBED
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
