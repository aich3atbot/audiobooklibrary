import enum
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ReadState(str, enum.Enum):
    NONE = "none"
    WANT_TO_READ = "want_to_read"
    READING = "reading"
    READ = "read"


class DownloadState(str, enum.Enum):
    NONE = "none"
    WANTED = "wanted"
    GRABBED = "grabbed"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    IMPORTED = "imported"
    FAILED = "failed"


def _enum_column(enum_cls, default):
    return mapped_column(
        Enum(enum_cls, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=default,
        nullable=False,
    )


class User(Base):
    """A login account. The admin account is virtual (checked against
    ADMIN_PASSWORD, never stored here); "admin" is a reserved username."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable identity exposed as the ABS API userId.
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    hardcover_token: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Per-user Hardcover sync cursor and outcome.
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_result: Mapped[str | None] = mapped_column(Text)


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    hardcover_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(500))

    books: Mapped[list["Book"]] = relationship(back_populates="author")


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    hardcover_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(500))

    books: Mapped[list["Book"]] = relationship(back_populates="series")


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    hardcover_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # Hardcover's user_book row id; needed to update/delete the shelf entry.
    hardcover_user_book_id: Mapped[int | None] = mapped_column(Integer)
    # Local read-state change not yet confirmed by Hardcover; pull sync must
    # not overwrite read_state/read_at while this is set.
    pending_push: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    title: Mapped[str] = mapped_column(String(1000))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"))
    series_index: Mapped[float | None] = mapped_column(Float)
    cover_url: Mapped[str | None] = mapped_column(Text)
    read_state: Mapped[ReadState] = _enum_column(ReadState, ReadState.NONE)
    read_at: Mapped[date | None] = mapped_column(Date)
    download_state: Mapped[DownloadState] = _enum_column(DownloadState, DownloadState.NONE)
    library_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    author: Mapped[Author] = relationship(back_populates="books")
    series: Mapped[Series | None] = relationship(back_populates="books")
    releases: Mapped[list["Release"]] = relationship(back_populates="book")
    audio_files: Mapped[list["AudioFile"]] = relationship(
        back_populates="book", order_by="AudioFile.index", cascade="all, delete-orphan"
    )
    media_progress: Mapped["MediaProgress | None"] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )
    bookmarks: Mapped[list["Bookmark"]] = relationship(
        back_populates="book", order_by="Bookmark.time", cascade="all, delete-orphan"
    )


class Release(Base):
    __tablename__ = "release"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), index=True)
    guid: Mapped[str] = mapped_column(Text)  # the release's details-page URL
    indexer: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(Integer)
    # What the download client is doing with it. info_hash is how we find the
    # torrent again; it is null on releases grabbed before the direct-torrent
    # rewrite, which fall back to matching the download folder by name.
    info_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    magnet_uri: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float | None] = mapped_column(Float)  # percent, 0–100
    grabbed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # grabbed -> downloading -> imported | failed | cancelled
    status: Mapped[str] = mapped_column(String(50), default="grabbed")
    error: Mapped[str | None] = mapped_column(Text)

    book: Mapped[Book] = relationship(back_populates="releases")


class AudioFile(Base):
    """An audio track of an imported book, for the ABS-compatible API.
    Populated by scanning library_path with mutagen."""

    __tablename__ = "audio_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # 1-based track order
    rel_path: Mapped[str] = mapped_column(Text)  # relative to book.library_path
    size: Mapped[int] = mapped_column(Integer)
    mtime_ms: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)  # seconds
    mime_type: Mapped[str] = mapped_column(String(50))
    # JSON [{id, start, end, title}] in seconds; usually only on m4b
    chapters_json: Mapped[str | None] = mapped_column(Text)

    book: Mapped[Book] = relationship(back_populates="audio_files")


class MediaProgress(Base):
    """Single-user listening progress per book (ABS clients sync this)."""

    __tablename__ = "media_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), unique=True, index=True)
    current_time: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    is_finished: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    book: Mapped[Book] = relationship(back_populates="media_progress")


class Bookmark(Base):
    """ABS player bookmarks: a labelled time position in a book."""

    __tablename__ = "bookmark"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), index=True)
    time: Mapped[float] = mapped_column(Float)  # seconds
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    book: Mapped[Book] = relationship(back_populates="bookmarks")


class AppState(Base):
    """Key-value store for sync cursors and other app state."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
