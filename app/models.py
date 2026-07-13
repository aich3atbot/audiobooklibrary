import enum
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
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


class Release(Base):
    __tablename__ = "release"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), index=True)
    prowlarr_guid: Mapped[str] = mapped_column(Text)
    indexer_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(Integer)
    seeders: Mapped[int | None] = mapped_column(Integer)
    grabbed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="grabbed")

    book: Mapped[Book] = relationship(back_populates="releases")


class AppState(Base):
    """Key-value store for sync cursors and other app state."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
