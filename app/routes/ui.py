from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Book

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/", response_class=HTMLResponse)
def library(request: Request, db: Session = Depends(get_db)):
    books = (
        db.execute(
            select(Book)
            .options(joinedload(Book.author), joinedload(Book.series))
            .order_by(Book.title)
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(request, "index.html", {"books": books})
