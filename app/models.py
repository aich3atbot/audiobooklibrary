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
    UniqueConstraint,
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
    GRABBED = "grabbed"
    DOWNLOADING = "downloading"
    IMPORTED = "imported"
    FAILED = "failed"


# The UI presents three download statuses: not present / downloading /
# available. The richer pipeline states map onto them; FAILED shows as
# not present plus a failure badge (details stay on the Activity page).
DISPLAY_DOWNLOADING = {
    DownloadState.GRABBED,
    DownloadState.DOWNLOADING,
}


def display_status(state: DownloadState) -> str:
    """Collapse one edition's pipeline state to the three UI statuses."""
    if state == DownloadState.IMPORTED:
        return "available"
    if state in DISPLAY_DOWNLOADING:
        return "downloading"
    return "not_present"


def book_status(book: "Book") -> str:
    """A book's aggregate status across its editions. Available wins (files
    on disk keep serving even while another edition downloads)."""
    if any(e.library_path for e in book.editions):
        return "available"
    if any(e.download_state in DISPLAY_DOWNLOADING for e in book.editions):
        return "downloading"
    return "not_present"


def book_failed(book: "Book") -> bool:
    return any(e.download_state == DownloadState.FAILED for e in book.editions)


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

    user_books: Mapped[list["UserBook"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    media_progress: Mapped[list["MediaProgress"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    bookmarks: Mapped[list["Bookmark"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    hardcover_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(500))
    # Hardcover's author photo. NULL = never looked up, "" = looked up and
    # there is none (so the backfill doesn't ask again every startup).
    image_url: Mapped[str | None] = mapped_column(Text)

    books: Mapped[list["Book"]] = relationship(back_populates="author")


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    hardcover_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(500))
    # hardcover.app/series/<slug>. NULL = never looked up, "" = looked up and
    # there is none — same three states as Author.image_url.
    hardcover_slug: Mapped[str | None] = mapped_column(Text)

    books: Mapped[list["Book"]] = relationship(back_populates="series")


class Book(Base):
    """Shared book metadata. Per-user shelf membership and read state live on
    UserBook; downloaded files and pipeline state live on Edition (a book may
    hold several audiobook editions). A Book may belong to no user's library
    (it is then just "available" in the shared store)."""

    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    hardcover_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # hardcover.app/books/<slug>; see Series.hardcover_slug for the states
    hardcover_slug: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(1000))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"))
    series_index: Mapped[float | None] = mapped_column(Float)
    cover_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    author: Mapped[Author] = relationship(back_populates="books")
    series: Mapped[Series | None] = relationship(back_populates="books")
    editions: Mapped[list["Edition"]] = relationship(
        back_populates="book", order_by="Edition.label", cascade="all, delete-orphan"
    )
    user_books: Mapped[list["UserBook"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )


class Edition(Base):
    """One audiobook recording of a Book (e.g. the Stephen Fry narration).
    Owns the on-disk files and the download pipeline state. label is the
    grouping name used in library folder names ("Stephen Fry", "Full Cast");
    "" is the unlabelled edition, which lives at the plain unsuffixed path.
    The unique constraint doubles as "at most one unlabelled edition"."""

    __tablename__ = "edition"
    __table_args__ = (UniqueConstraint("book_id", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), index=True
    )
    # Hardcover's edition id when the user picked from the live editions list;
    # null for free-form labels and pre-edition rows.
    hardcover_edition_id: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # Display narrators from the Hardcover edition ("Stephen Fry").
    narrator: Mapped[str] = mapped_column(String(500), default="", server_default="")
    download_state: Mapped[DownloadState] = _enum_column(DownloadState, DownloadState.NONE)
    library_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    book: Mapped[Book] = relationship(back_populates="editions")
    releases: Mapped[list["Release"]] = relationship(
        back_populates="edition", cascade="all, delete-orphan"
    )
    audio_files: Mapped[list["AudioFile"]] = relationship(
        back_populates="edition", order_by="AudioFile.index", cascade="all, delete-orphan"
    )
    media_progress: Mapped[list["MediaProgress"]] = relationship(
        back_populates="edition", cascade="all, delete-orphan"
    )
    bookmarks: Mapped[list["Bookmark"]] = relationship(
        back_populates="edition", order_by="Bookmark.time", cascade="all, delete-orphan"
    )


class UserBook(Base):
    """A book's membership in one user's library: their Hardcover shelf entry
    and read state. Mirrors the user's Hardcover library (which is the source
    of truth for read state)."""

    __tablename__ = "user_book"
    __table_args__ = (UniqueConstraint("user_id", "book_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    book_id: Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), index=True
    )
    # Hardcover's user_book row id; needed to update/delete the shelf entry.
    hardcover_user_book_id: Mapped[int | None] = mapped_column(Integer)
    # Local read-state change not yet confirmed by Hardcover; pull sync must
    # not overwrite read_state/read_at while this is set.
    pending_push: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    read_state: Mapped[ReadState] = _enum_column(ReadState, ReadState.NONE)
    read_at: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="user_books")
    book: Mapped[Book] = relationship(back_populates="user_books")


class Release(Base):
    __tablename__ = "release"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable only as SQLite pragmatism; code always sets it (the migration
    # backfills legacy rows).
    edition_id: Mapped[int | None] = mapped_column(ForeignKey("edition.id"), index=True)
    # Who grabbed it (attribution on the shared Activity page); null once the
    # user is deleted, or on releases predating multi-user.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
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

    edition: Mapped[Edition] = relationship(back_populates="releases")
    user: Mapped[User | None] = relationship()

    @property
    def book(self) -> Book:
        return self.edition.book


class AudioFile(Base):
    """An audio track of an imported edition, for the ABS-compatible API.
    Populated by scanning library_path with mutagen."""

    __tablename__ = "audio_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("edition.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # 1-based track order
    rel_path: Mapped[str] = mapped_column(Text)  # relative to edition.library_path
    size: Mapped[int] = mapped_column(Integer)
    mtime_ms: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)  # seconds
    mime_type: Mapped[str] = mapped_column(String(50))
    # JSON [{id, start, end, title}] in seconds; usually only on m4b
    chapters_json: Mapped[str | None] = mapped_column(Text)

    edition: Mapped[Edition] = relationship(back_populates="audio_files")


class MediaProgress(Base):
    """One user's listening progress for one edition (ABS clients sync this).
    Read state stays book-level on UserBook; finishing any edition marks the
    book read."""

    __tablename__ = "media_progress"
    __table_args__ = (UniqueConstraint("user_id", "edition_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    edition_id: Mapped[int] = mapped_column(ForeignKey("edition.id"), index=True)
    current_time: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    is_finished: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="media_progress")
    edition: Mapped[Edition] = relationship(back_populates="media_progress")


class Bookmark(Base):
    """ABS player bookmarks: a user's labelled time position in an edition."""

    __tablename__ = "bookmark"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    edition_id: Mapped[int] = mapped_column(ForeignKey("edition.id"), index=True)
    time: Mapped[float] = mapped_column(Float)  # seconds
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="bookmarks")
    edition: Mapped[Edition] = relationship(back_populates="bookmarks")


class AppState(Base):
    """Key-value store for sync cursors and other app state."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
