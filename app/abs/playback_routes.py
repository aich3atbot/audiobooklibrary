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
from app.models import AudioFile, Book

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_abs_user)])

# In-memory playback sessions: single user, sessions die with the process
# (matches ABS, which also keeps open sessions in memory).
open_sessions: dict[str, dict[str, Any]] = {}


def _get_book(db: Session, item_id: str) -> Book:
    book = catalogue.get_book_by_item_id(db, item_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return book


def _get_audio_file(db: Session, book: Book, ino: str) -> tuple[AudioFile, Path]:
    try:
        file_id = int(ino)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    file = db.get(AudioFile, file_id)
    if file is None or file.book_id != book.id:
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(book.library_path) / file.rel_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return file, path


@router.post("/items/{item_id}/play")
async def start_playback(item_id: str, request: Request, db: Session = Depends(get_db)):
    book = _get_book(db, item_id)
    if not book.audio_files:
        raise HTTPException(status_code=500, detail="Item has no audio files")
    try:
        body = await request.json()
    except Exception:
        body = {}

    progress = book.media_progress
    start_time = 0.0
    if progress and not progress.is_finished:
        start_time = progress.current_time

    now = datetime.now(timezone.utc)
    device_info = body.get("deviceInfo") or {}
    device_info.setdefault("id", device_info.get("deviceId") or "unknown-device")
    session_id = f"play_{uuid.uuid4().hex}"
    duration = catalogue.book_duration(book)

    open_sessions[session_id] = {"book_id": book.id, "started_at": payloads.now_ms()}

    return {
        "id": session_id,
        "userId": payloads.tokens.user_id(),
        "libraryId": payloads.LIBRARY_ID,
        "libraryItemId": item_id,
        "bookId": f"bk_{book.id}",
        "episodeId": None,
        "mediaType": "book",
        "mediaMetadata": catalogue.metadata_expanded(book),
        "chapters": catalogue.book_chapters(book),
        "displayTitle": book.title,
        "displayAuthor": book.author.name,
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
        "audioTracks": catalogue.audio_tracks(book),
        "libraryItem": catalogue.item_expanded(book),
    }


@router.get("/items/{item_id}/file/{ino}")
def stream_file(item_id: str, ino: str, db: Session = Depends(get_db)):
    book = _get_book(db, item_id)
    file, path = _get_audio_file(db, book, ino)
    # FileResponse handles HTTP Range (seek) automatically.
    return FileResponse(path, media_type=file.mime_type)


@router.get("/items/{item_id}/file/{ino}/download")
def download_file(item_id: str, ino: str, db: Session = Depends(get_db)):
    book = _get_book(db, item_id)
    file, path = _get_audio_file(db, book, ino)
    return FileResponse(path, media_type=file.mime_type, filename=path.name)


def _session_book(db: Session, session_id: str) -> Book:
    session = open_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    book = db.get(Book, session["book_id"])
    if book is None:
        raise HTTPException(status_code=404, detail="Session book not found")
    return book


@router.post("/session/{session_id}/sync")
async def sync_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    book = _session_book(db, session_id)
    apply_progress(
        db, book, current_time=body.get("currentTime"), duration=body.get("duration")
    )
    return Response(status_code=200)


@router.post("/session/{session_id}/close")
async def close_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if session_id in open_sessions:
        if body.get("currentTime") is not None:
            book = _session_book(db, session_id)
            apply_progress(
                db, book, current_time=body.get("currentTime"), duration=body.get("duration")
            )
        del open_sessions[session_id]
    return Response(status_code=200)


def _apply_local_session(db: Session, session: dict[str, Any]) -> bool:
    item_id = session.get("libraryItemId") or ""
    book = catalogue.get_book_by_item_id(db, item_id)
    if book is None:
        logger.warning("Local session sync for unknown item %s", item_id)
        return False
    apply_progress(
        db,
        book,
        current_time=session.get("currentTime"),
        duration=session.get("duration"),
    )
    return True


@router.post("/session/local")
async def sync_local_session(request: Request, db: Session = Depends(get_db)):
    session = await request.json()
    _apply_local_session(db, session)
    return {}


@router.post("/session/local-all")
async def sync_local_sessions(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    results = []
    for session in body.get("sessions") or []:
        success = _apply_local_session(db, session)
        results.append({"id": session.get("id"), "success": success})
    return {"results": results}


@router.patch("/me/progress/{item_id}")
async def update_progress(item_id: str, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    book = _get_book(db, item_id)
    apply_progress(
        db,
        book,
        current_time=body.get("currentTime"),
        duration=body.get("duration"),
        is_finished=body.get("isFinished"),
    )
    return Response(status_code=200)


@router.get("/me/progress/{item_id}")
def get_progress(item_id: str, db: Session = Depends(get_db)):
    book = _get_book(db, item_id)
    if book.media_progress is None:
        raise HTTPException(status_code=404, detail="No progress")
    return catalogue.progress_json(book.media_progress)
