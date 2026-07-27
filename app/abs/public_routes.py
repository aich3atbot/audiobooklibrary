"""ABS API: the endpoints Audiobookshelf serves without authentication.

Upstream exempts `GET /api/items/:id/cover` from auth (`Auth.ignorePatterns`),
and since server 2.17.0 the official apps send no token with cover requests at
all — Android loads them through Glide's bare `HttpURLConnection`, which
carries no headers. Direct play likewise moved (server 2.22.0) to
`GET /public/session/:id/track/:index`, where the playback session id *is* the
credential. Both are version-gated in the app on the `serverVersion` we
advertise, so neither is optional. See docs/abs-api-contract.md.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.abs import catalogue
from app.abs.playback_routes import open_sessions
from app.db import get_db
from app.models import Edition

router = APIRouter()


@router.get("/api/items/{item_id}/cover")
def item_cover(item_id: str, db: Session = Depends(get_db)):
    edition = catalogue.get_edition_by_item_id(db, item_id)
    if edition is None:
        raise HTTPException(status_code=404, detail="Item not found")
    cover = catalogue.find_cover_file(edition)
    if cover is not None:
        return FileResponse(cover)
    if edition.book.cover_url:
        return RedirectResponse(url=edition.book.cover_url, status_code=302)
    raise HTTPException(status_code=404, detail="No cover")


@router.get("/public/session/{session_id}/track/{index}")
def session_track(session_id: str, index: int, db: Session = Depends(get_db)):
    """Stream one track of an open playback session (direct play)."""
    session = open_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    edition = db.get(Edition, session["edition_id"])
    if edition is None or not edition.library_path:
        raise HTTPException(status_code=404, detail="Session book not found")
    file = next((f for f in edition.audio_files if f.index == index), None)
    if file is None:
        raise HTTPException(status_code=404, detail="Track not found")
    path = Path(edition.library_path) / file.rel_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")
    # FileResponse handles HTTP Range (seek) automatically.
    return FileResponse(path, media_type=file.mime_type)
