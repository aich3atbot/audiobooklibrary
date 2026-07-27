"""ABS API: playback sessions, audio file streaming, and progress sync."""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.abs import catalogue, payloads
from app.abs.deps import require_abs_user
from app.abs.progress import apply_progress
from app.db import get_db
from app.models import AudioFile, Bookmark, Edition, MediaProgress, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_abs_user)])

# In-memory playback sessions, owned by the user who opened them; sessions
# die with the process (matches ABS, which also keeps them in memory).
open_sessions: dict[str, dict[str, Any]] = {}


def _get_edition(db: Session, item_id: str) -> Edition:
    edition = catalogue.get_edition_by_item_id(db, item_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return edition


def _get_audio_file(db: Session, edition: Edition, ino: str) -> tuple[AudioFile, Path]:
    try:
        file_id = int(ino)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    file = db.get(AudioFile, file_id)
    if file is None or file.edition_id != edition.id:
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(edition.library_path) / file.rel_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return file, path


@router.post("/items/{item_id}/play")
async def start_playback(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    edition = _get_edition(db, item_id)
    if not edition.audio_files:
        raise HTTPException(status_code=500, detail="Item has no audio files")
    try:
        body = await request.json()
    except Exception:
        body = {}

    progress = catalogue.get_progress(db, user, edition.id)
    start_time = 0.0
    if progress and not progress.is_finished:
        start_time = progress.current_time

    now = datetime.now(timezone.utc)
    device_info = body.get("deviceInfo") or {}
    device_info.setdefault("id", device_info.get("deviceId") or "unknown-device")
    session_id = f"play_{uuid.uuid4().hex}"
    duration = catalogue.edition_duration(edition)

    # One open session per user+device: clients re-open a session on every
    # playback error, so without this a retry storm grows the dict unbounded.
    device_id = device_info["id"]
    for stale_id, stale in list(open_sessions.items()):
        if stale["user_id"] == user.id and stale.get("device_id") == device_id:
            del open_sessions[stale_id]

    open_sessions[session_id] = {
        "edition_id": edition.id,
        "user_id": user.id,
        "device_id": device_id,
        "started_at": payloads.now_ms(),
    }

    return {
        "id": session_id,
        "userId": user.uuid,
        "libraryId": payloads.LIBRARY_ID,
        "libraryItemId": item_id,
        "bookId": f"bk_{edition.id}",
        "episodeId": None,
        "mediaType": "book",
        "mediaMetadata": catalogue.metadata_expanded(edition),
        "chapters": catalogue.edition_chapters(edition),
        "displayTitle": catalogue.display_title(edition),
        "displayAuthor": edition.book.author.name,
        "coverPath": f"/api/items/{item_id}/cover",
        "duration": duration,
        "playMethod": 0,  # direct play
        "mediaPlayer": body.get("mediaPlayer") or "unknown",
        "deviceInfo": device_info,
        "serverVersion": payloads.SERVER_VERSION,
        "date": now.strftime("%Y-%m-%d"),
        "dayOfWeek": now.strftime("%A"),
        "timeListening": 0,
        "startTime": start_time,
        "currentTime": start_time,
        "startedAt": payloads.now_ms(),
        "updatedAt": payloads.now_ms(),
        "audioTracks": catalogue.audio_tracks(edition),
        "libraryItem": catalogue.item_expanded(edition),
    }


@router.get("/items/{item_id}/file/{ino}")
def stream_file(item_id: str, ino: str, db: Session = Depends(get_db)):
    edition = _get_edition(db, item_id)
    file, path = _get_audio_file(db, edition, ino)
    # FileResponse handles HTTP Range (seek) automatically.
    return FileResponse(path, media_type=file.mime_type)


@router.get("/items/{item_id}/file/{ino}/download")
def download_file(item_id: str, ino: str, db: Session = Depends(get_db)):
    edition = _get_edition(db, item_id)
    file, path = _get_audio_file(db, edition, ino)
    return FileResponse(path, media_type=file.mime_type, filename=path.name)


def _session_edition(db: Session, user: User, session_id: str) -> Edition:
    session = open_sessions.get(session_id)
    if session is None or session.get("user_id") != user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    edition = db.get(Edition, session["edition_id"])
    if edition is None:
        raise HTTPException(status_code=404, detail="Session book not found")
    return edition


@router.post("/session/{session_id}/sync")
async def sync_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    body = await request.json()
    edition = _session_edition(db, user, session_id)
    apply_progress(
        db, user, edition, current_time=body.get("currentTime"), duration=body.get("duration")
    )
    return Response(status_code=200)


@router.post("/session/{session_id}/close")
async def close_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if session_id in open_sessions:
        if body.get("currentTime") is not None:
            edition = _session_edition(db, user, session_id)
            apply_progress(
                db,
                user,
                edition,
                current_time=body.get("currentTime"),
                duration=body.get("duration"),
            )
        del open_sessions[session_id]
    return Response(status_code=200)


def _apply_local_session(db: Session, user: User, session: dict[str, Any]) -> bool:
    item_id = session.get("libraryItemId") or ""
    edition = catalogue.get_edition_by_item_id(db, item_id)
    if edition is None:
        logger.warning("Local session sync for unknown item %s", item_id)
        return False
    apply_progress(
        db,
        user,
        edition,
        current_time=session.get("currentTime"),
        duration=session.get("duration"),
    )
    return True


@router.post("/session/local")
async def sync_local_session(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    session = await request.json()
    _apply_local_session(db, user, session)
    return {}


@router.post("/session/local-all")
async def sync_local_sessions(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    body = await request.json()
    results = []
    for session in body.get("sessions") or []:
        success = _apply_local_session(db, user, session)
        results.append({"id": session.get("id"), "success": success})
    return {"results": results}


@router.patch("/me/progress/{item_id}")
async def update_progress(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    body = await request.json()
    edition = _get_edition(db, item_id)
    current_time = body.get("currentTime")
    duration = body.get("duration")
    if current_time is None and body.get("progress") is not None:
        # clients may send only a completion fraction
        existing = catalogue.get_progress(db, user, edition.id)
        effective_duration = (
            duration
            or (existing.duration if existing else 0)
            or catalogue.edition_duration(edition)
        )
        if effective_duration:
            current_time = float(body["progress"]) * effective_duration
    apply_progress(
        db,
        user,
        edition,
        current_time=current_time,
        duration=duration,
        is_finished=body.get("isFinished"),
    )
    return Response(status_code=200)


@router.get("/me/progress/{item_id}")
def get_progress(
    item_id: str, db: Session = Depends(get_db), user: User = Depends(require_abs_user)
):
    edition = _get_edition(db, item_id)
    progress = catalogue.get_progress(db, user, edition.id)
    if progress is None:
        raise HTTPException(status_code=404, detail="No progress")
    return catalogue.progress_json(progress, user)


def _user_bookmark(
    db: Session, user: User, edition: Edition, time: float
) -> Bookmark | None:
    from sqlalchemy import select

    return db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.edition_id == edition.id,
            Bookmark.time == time,
        )
    )


@router.post("/me/item/{item_id}/bookmark")
async def create_bookmark(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    body = await request.json()
    edition = _get_edition(db, item_id)
    time = body.get("time")
    title = body.get("title")
    if time is None or not isinstance(title, str) or not title:
        raise HTTPException(status_code=400, detail="Invalid time or title")
    bookmark = _user_bookmark(db, user, edition, float(time))
    if bookmark is None:
        bookmark = Bookmark(user_id=user.id, edition=edition, time=float(time), title=title)
        db.add(bookmark)
    else:
        bookmark.title = title
    db.commit()
    return catalogue.bookmark_json(bookmark)


@router.patch("/me/item/{item_id}/bookmark")
async def update_bookmark(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    body = await request.json()
    edition = _get_edition(db, item_id)
    time = body.get("time")
    bookmark = _user_bookmark(db, user, edition, float(time)) if time is not None else None
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if isinstance(body.get("title"), str) and body["title"]:
        bookmark.title = body["title"]
    db.commit()
    return catalogue.bookmark_json(bookmark)


@router.delete("/me/item/{item_id}/bookmark/{time}")
def delete_bookmark(
    item_id: str,
    time: float,
    db: Session = Depends(get_db),
    user: User = Depends(require_abs_user),
):
    edition = _get_edition(db, item_id)
    bookmark = _user_bookmark(db, user, edition, time)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    db.delete(bookmark)
    db.commit()
    return Response(status_code=200)


@router.get("/me/listening-stats")
def listening_stats(db: Session = Depends(get_db)):
    # We don't persist listening sessions; zeroed stats keep the app's
    # stats screen rendering.
    return {
        "totalTime": 0,
        "items": {},
        "days": {},
        "dayOfWeek": {},
        "today": 0,
        "recentSessions": [],
    }


@router.get("/me/stats/year/{year}")
def year_stats(
    year: int, db: Session = Depends(get_db), user: User = Depends(require_abs_user)
):
    finished = (
        db.query(MediaProgress)
        .filter(MediaProgress.user_id == user.id)
        .filter(MediaProgress.is_finished.is_(True))
        .filter(MediaProgress.finished_at.is_not(None))
        .all()
    )
    finished_in_year = [p for p in finished if p.finished_at.year == year]
    return {
        "totalListeningSessions": 0,
        "totalListeningTime": 0,
        "totalBookListeningTime": 0,
        "totalPodcastListeningTime": 0,
        "topAuthors": [],
        "topGenres": [],
        "mostListenedNarrator": None,
        "mostListenedMonth": None,
        "numBooksFinished": len(finished_in_year),
        "numBooksListened": len(finished_in_year),
        "longestAudiobookFinished": None,
        "booksWithCovers": [],
        "finishedBooksWithCovers": [],
    }
