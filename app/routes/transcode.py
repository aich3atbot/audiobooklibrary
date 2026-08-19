"""Converting an MP3 edition into a single chaptered m4b, from the book detail
page. The work itself runs in the background worker (app/services/transcode.py);
these routes queue it, poll it and cancel it."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import get_current_user
from app.db import get_db
from app.models import TRANSCODE_ACTIVE, Edition, TranscodeJob, User
from app.services.transcode import active_job, mp3_sources, queue_job, transcode_blocked
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_edition(db: Session, edition_id: int) -> Edition:
    edition = db.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="edition not found")
    return edition


def _get_job(db: Session, job_id: int) -> TranscodeJob:
    job = db.get(TranscodeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="transcode job not found")
    return job


def status_context(db: Session, edition: Edition, refresh: bool = False) -> dict:
    """What the status panel needs.

    The panel only appears for an edition that *is* a pile of MP3s — an m4b
    edition should not be nagged about a conversion that does not apply to it.
    Beyond that it shows the running job, the last finished one (so a failure
    stays visible until dismissed), or the button."""
    from pathlib import Path

    job = active_job(db, edition)
    if job is None:
        job = (
            db.query(TranscodeJob)
            .filter(TranscodeJob.edition_id == edition.id)
            .order_by(TranscodeJob.id.desc())
            .first()
        )
    active = job is not None and job.state in TRANSCODE_ACTIVE
    root = Path(edition.library_path) if edition.library_path else None
    applicable = root is not None and root.is_dir() and mp3_sources(root) is not None
    return {
        "edition": edition,
        "job": job,
        "active": active,
        "applicable": applicable,
        # why it cannot start right now (ffmpeg missing, a download in flight)
        "blocked": None if active or not applicable else transcode_blocked(db, edition),
        # set by the poll when a job has just finished, so the panel pulls the
        # new file list in once; the reloaded fragment renders without it, so
        # this cannot loop
        "refresh": refresh,
    }


@router.get("/editions/{edition_id}/transcode", response_class=HTMLResponse)
async def transcode_status(
    edition_id: int, request: Request, db: Session = Depends(get_db)
):
    """The status panel, polled while a job is queued or running. When the job
    finishes it stops polling and pulls the file list back in, which is how the
    single m4b replaces the MP3 table without a page reload."""
    edition = _get_edition(db, edition_id)
    context = await run_in_threadpool(status_context, db, edition, True)
    return templates.TemplateResponse(request, "_transcode_status.html", context)


@router.get("/editions/{edition_id}/transcode/confirm", response_class=HTMLResponse)
async def transcode_confirm(
    edition_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The confirm dialog. Deleting the MP3s is irreversible, so it says how
    many files go in, what comes out, and where the chapters will come from."""
    edition = _get_edition(db, edition_id)

    def build() -> dict:
        from pathlib import Path

        from app.config import get_settings
        from app.services.transcode import (
            chapter_plan,
            mp3_sources,
            probe_sources,
            target_bitrate,
        )

        blocked = transcode_blocked(db, edition)
        if blocked:
            return {"edition": edition, "error": blocked}
        paths = [Path(edition.library_path) / f.rel_path for f in edition.audio_files]
        sources = probe_sources([p for p in paths if p.is_file()])
        # Tag durations are good enough to *describe* the job; the decode pass
        # that places the chapters happens in the worker.
        durations = [s.duration for s in sources]
        chapters, sidecar = chapter_plan(edition, durations)
        return {
            "edition": edition,
            "error": None,
            "count": len(mp3_sources(Path(edition.library_path)) or []),
            "bitrate": target_bitrate(sources, get_settings().transcode_bitrate),
            "duration": sum(durations),
            "chapters": len(chapters),
            "sidecar": sidecar.name if sidecar else None,
            "output": f"{Path(edition.library_path).name}.m4b",
        }

    context = await run_in_threadpool(build)
    return templates.TemplateResponse(request, "_transcode_confirm.html", context)


@router.post("/editions/{edition_id}/transcode", response_class=HTMLResponse)
async def start_transcode(
    edition_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Queue the conversion and swap the modal for the status panel."""
    edition = _get_edition(db, edition_id)
    blocked = await run_in_threadpool(transcode_blocked, db, edition)
    if blocked:
        return templates.TemplateResponse(
            request, "_transcode_confirm.html", {"edition": edition, "error": blocked}
        )
    queue_job(db, edition, user)
    context = await run_in_threadpool(status_context, db, edition)
    return templates.TemplateResponse(request, "_transcode_started.html", context)


@router.post("/transcodes/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_transcode(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ask the worker to stop. A queued job never starts; a running one is
    stopped at its next progress checkpoint (the worker polls this flag), so
    the state here stays what it is until the worker acts on it."""
    job = _get_job(db, job_id)
    if job.state in TRANSCODE_ACTIVE:
        job.cancel_requested = True
        db.commit()
    if request.headers.get("hx-request"):
        context = await run_in_threadpool(status_context, db, job.edition)
        return templates.TemplateResponse(request, "_transcode_status.html", context)
    return RedirectResponse(url="/activity", status_code=303)


@router.post("/transcodes/{job_id}/dismiss")
async def dismiss_transcode(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Clear a finished job off the Activity page. Only the record goes — the
    files it produced (or left alone) are not touched."""
    job = _get_job(db, job_id)
    if job.state in TRANSCODE_ACTIVE:
        raise HTTPException(status_code=409, detail="that job is still running")
    db.delete(job)
    db.commit()
    return RedirectResponse(url="/activity", status_code=303)
