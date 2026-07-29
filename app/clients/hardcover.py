"""Hardcover GraphQL API client.

The API is beta and its schema shifts; the queries below were verified against
the live API. `me` returns a list of users, not a single object.
"""

from typing import Any

import httpx

API_URL = "https://api.hardcover.app/v1/graphql"
PAGE_SIZE = 100

USER_BOOK_FIELDS = """
      id
      status_id
      last_read_date
      book {
        id
        title
        slug
        cached_image
        contributions {
          author { id name cached_image }
        }
        book_series {
          position
          featured
          series { id name slug }
        }
      }
      user_book_reads { finished_at }
"""

USER_BOOKS_QUERY = f"""
query UserBooks($limit: Int!, $offset: Int!) {{
  me {{
    user_books(limit: $limit, offset: $offset, order_by: {{id: asc}}) {{
{USER_BOOK_FIELDS}
    }}
  }}
}}
"""

USER_BOOK_BY_BOOK_QUERY = f"""
query UserBookByBook($bookId: Int!) {{
  me {{
    user_books(where: {{book_id: {{_eq: $bookId}}}}, limit: 1) {{
{USER_BOOK_FIELDS}
    }}
  }}
}}
"""

# The inner `book` shape again, via the public books root query — verified
# live 2026-07. Used to import books nobody has shelved.
BOOK_BY_ID_QUERY = """
query BookById($id: Int!) {
  books(where: {id: {_eq: $id}}) {
    id
    title
    slug
    cached_image
    contributions {
      author { id name cached_image }
    }
    book_series {
      position
      featured
      series { id name slug }
    }
  }
}
"""

# All books in a series, verified live 2026-07. A series carries every
# edition/boxset (HP: 112 rows for 7 books); canonical_id null keeps canonical
# books only, and the users_count ordering lets fetch_series keep the most
# popular edition per position.
SERIES_BOOKS_QUERY = """
query SeriesBooks($sid: Int!) {
  series(where: {id: {_eq: $sid}}) { name slug }
  book_series(where: {series_id: {_eq: $sid}, book: {canonical_id: {_is_null: true}}},
              order_by: [{position: asc}, {book: {users_count: desc}}]) {
    position
    book {
      id
      title
      release_year
      cached_image
      users_count
      contributions {
        author { id name cached_image }
      }
    }
  }
}
"""

# A book's audiobook editions, verified live 2026-07-18 (see plan.md):
# reading_format_id 2 ("Listened") is the audiobook format. Ordering by
# users_count matters — books can carry dozens of junk/foreign editions.
AUDIOBOOK_FORMAT_ID = 2

EDITIONS_QUERY = """
query AudioEditions($bookId: Int!) {
  editions(where: {book_id: {_eq: $bookId}, reading_format_id: {_eq: 2}},
           order_by: {users_count: desc}) {
    id
    title
    subtitle
    edition_format
    asin
    isbn_13
    audio_seconds
    release_date
    users_count
    publisher { name }
    contributions { contribution author { id name } }
  }
}
"""

# The contribution role string is inconsistent in the wild: usually
# "Narrator", but also "narrator", "Reader", "Sprecher"; null for the author.
NARRATOR_ROLES = {"narrator", "reader", "sprecher"}


# Author photos for authors already in the library, verified live 2026-07-28.
# `cached_image` has the same shape as a book's.
AUTHOR_IMAGES_QUERY = """
query AuthorImages($ids: [Int!]) {
  authors(where: {id: {_in: $ids}}) {
    id
    cached_image
  }
}
"""


# Slugs for books/series stored before we asked for them, verified live
# 2026-07-29. hardcover.app routes on the slug, so a link needs it.
BOOK_SLUGS_QUERY = """
query BookSlugs($ids: [Int!]) {
  books(where: {id: {_in: $ids}}) {
    id
    slug
  }
}
"""

SERIES_SLUGS_QUERY = """
query SeriesSlugs($ids: [Int!]) {
  series(where: {id: {_in: $ids}}) {
    id
    slug
  }
}
"""


# search results is raw Typesense JSON (jsonb), not typed GraphQL fields
SEARCH_QUERY = """
query Search($query: String!, $perPage: Int!, $page: Int!) {
  search(query: $query, query_type: "Book", per_page: $perPage, page: $page) {
    results
  }
}
"""


INSERT_USER_BOOK = """
mutation InsertUserBook($object: UserBookCreateInput!) {
  insert_user_book(object: $object) { id error }
}
"""

UPDATE_USER_BOOK = """
mutation UpdateUserBook($id: Int!, $object: UserBookUpdateInput!) {
  update_user_book(id: $id, object: $object) { id error }
}
"""

DELETE_USER_BOOK = """
mutation DeleteUserBook($id: Int!) {
  delete_user_book(id: $id) { id }
}
"""


class HardcoverError(RuntimeError):
    pass


class HardcoverClient:
    def __init__(self, token: str, base_url: str = API_URL, timeout: float = 30.0):
        # Tolerate tokens pasted with the "Bearer " prefix already included.
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        self._url = base_url
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HardcoverClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(
            self._url, json={"query": query, "variables": variables or {}}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise HardcoverError(f"Hardcover query failed: {payload['errors']}")
        return payload["data"]

    def fetch_author_images(self, author_ids: list[int]) -> dict[int, str]:
        """Author photo urls keyed by Hardcover author id; authors without one
        are simply absent."""
        data = self.execute(AUTHOR_IMAGES_QUERY, {"ids": author_ids})
        images = {}
        for row in data.get("authors") or []:
            url = (row.get("cached_image") or {}).get("url")
            if url:
                images[row["id"]] = url
        return images

    def fetch_book_slugs(self, book_ids: list[int]) -> dict[int, str]:
        """hardcover.app slugs keyed by Hardcover book id; books without one
        are simply absent."""
        return self._fetch_slugs(BOOK_SLUGS_QUERY, "books", book_ids)

    def fetch_series_slugs(self, series_ids: list[int]) -> dict[int, str]:
        """hardcover.app slugs keyed by Hardcover series id."""
        return self._fetch_slugs(SERIES_SLUGS_QUERY, "series", series_ids)

    def _fetch_slugs(self, query: str, key: str, ids: list[int]) -> dict[int, str]:
        data = self.execute(query, {"ids": ids})
        return {
            row["id"]: row["slug"] for row in data.get(key) or [] if row.get("slug")
        }

    def insert_user_book(
        self, book_id: int, status_id: int, last_read_date: str | None = None
    ) -> int:
        """Add a book to the user's Hardcover shelf; returns the user_book id."""
        obj: dict[str, Any] = {"book_id": book_id, "status_id": status_id}
        if last_read_date:
            obj["last_read_date"] = last_read_date
        data = self.execute(INSERT_USER_BOOK, {"object": obj})
        result = data["insert_user_book"]
        if result.get("error"):
            raise HardcoverError(f"insert_user_book failed: {result['error']}")
        return result["id"]

    def update_user_book(
        self, user_book_id: int, status_id: int, last_read_date: str | None = None
    ) -> None:
        obj: dict[str, Any] = {"status_id": status_id}
        if last_read_date:
            obj["last_read_date"] = last_read_date
        data = self.execute(UPDATE_USER_BOOK, {"id": user_book_id, "object": obj})
        result = data["update_user_book"]
        if result.get("error"):
            raise HardcoverError(f"update_user_book failed: {result['error']}")

    def delete_user_book(self, user_book_id: int) -> None:
        self.execute(DELETE_USER_BOOK, {"id": user_book_id})

    def me(self) -> dict[str, Any]:
        """Return the authenticated user (id, username); raises if the token
        is bad. Note: Hardcover's `me` returns a list."""
        data = self.execute("{ me { id username } }")
        users = data.get("me") or []
        if not users:
            raise HardcoverError("token accepted but no user returned")
        return users[0]

    def fetch_user_books(self) -> list[dict[str, Any]]:
        """Fetch the authenticated user's full library, paginated."""
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = self.execute(USER_BOOKS_QUERY, {"limit": PAGE_SIZE, "offset": offset})
            users = data.get("me") or []
            page = users[0]["user_books"] if users else []
            entries.extend(page)
            if len(page) < PAGE_SIZE:
                return entries
            offset += PAGE_SIZE

    def fetch_user_book(self, book_id: int) -> dict[str, Any] | None:
        """Fetch the user's shelf entry for one book, in the same shape as
        fetch_user_books entries. None if the book isn't on their shelf."""
        data = self.execute(USER_BOOK_BY_BOOK_QUERY, {"bookId": book_id})
        users = data.get("me") or []
        entries = users[0]["user_books"] if users else []
        return entries[0] if entries else None

    def fetch_book(self, book_id: int) -> dict[str, Any] | None:
        """Fetch one book's metadata (no shelf entry required), in the same
        inner `book` shape as fetch_user_books entries. None if unknown."""
        data = self.execute(BOOK_BY_ID_QUERY, {"id": book_id})
        books = data.get("books") or []
        return books[0] if books else None

    def fetch_editions(self, book_id: int) -> list[dict[str, Any]]:
        """A book's audiobook editions, most-shelved first. Each entry carries
        the narrators and a default grouping label ("Stephen Fry"; "Full
        Cast" when a big ensemble is credited)."""
        data = self.execute(EDITIONS_QUERY, {"bookId": book_id})
        return [_parse_edition(row) for row in data.get("editions") or []]

    def search_books(
        self, query: str, per_page: int = 25, page: int = 1
    ) -> list[dict[str, Any]]:
        data = self.execute(
            SEARCH_QUERY, {"query": query, "perPage": per_page, "page": page}
        )
        results = (data.get("search") or {}).get("results") or {}
        return [_parse_search_document(hit["document"]) for hit in results.get("hits", [])]

    def fetch_series(
        self, series_id: int
    ) -> tuple[str | None, str | None, list[dict[str, Any]]]:
        """The series name, its hardcover.app slug, and its books, one per
        position (most-shelved canonical edition wins), in the search-result
        dict shape."""
        data = self.execute(SERIES_BOOKS_QUERY, {"sid": series_id})
        series_rows = data.get("series") or []
        name = series_rows[0]["name"] if series_rows else None
        slug = series_rows[0].get("slug") if series_rows else None
        books = []
        seen_positions: set[float] = set()
        # ponytail: null-position rows (companions) aren't deduped; dedupe by
        # book id if duplicate editions of those ever show up.
        for row in data.get("book_series") or []:
            position = row.get("position")
            if position is not None:
                if position in seen_positions:
                    continue
                seen_positions.add(position)
            book = row["book"]
            contributions = book.get("contributions") or []
            books.append(
                {
                    "hardcover_id": int(book["id"]),
                    "title": book["title"],
                    "authors": [c["author"]["name"] for c in contributions if c.get("author")][:1],
                    "series_id": series_id,
                    "series_name": name,
                    "series_position": position,
                    "cover_url": (book.get("cached_image") or {}).get("url"),
                    "release_year": book.get("release_year"),
                    "has_audiobook": False,
                    "users_count": book.get("users_count") or 0,
                }
            )
        return name, slug, books


def _parse_search_document(doc: dict[str, Any]) -> dict[str, Any]:
    # author_names mixes authors with narrators; contribution_types (aligned
    # by index with contributions) tells them apart.
    types = doc.get("contribution_types") or []
    authors = []
    for i, contribution in enumerate(doc.get("contributions") or []):
        ctype = types[i] if i < len(types) else "Author"
        if (ctype or "Author") == "Author" and contribution.get("author"):
            authors.append(contribution["author"]["name"])
    if not authors:
        authors = (doc.get("author_names") or [])[:1]

    featured = doc.get("featured_series") or {}
    featured_series = featured.get("series") or {}
    image = doc.get("image") or {}
    return {
        "hardcover_id": int(doc["id"]),
        "slug": doc.get("slug"),
        "title": doc["title"],
        "authors": authors,
        "series_id": int(featured_series["id"]) if featured_series.get("id") is not None else None,
        "series_name": featured_series.get("name"),
        "series_slug": featured_series.get("slug"),
        "series_position": featured.get("position"),
        "cover_url": image.get("url"),
        "release_year": doc.get("release_year"),
        "has_audiobook": bool(doc.get("has_audiobook")),
        "users_count": doc.get("users_count") or 0,
    }


def _parse_edition(row: dict[str, Any]) -> dict[str, Any]:
    narrators = [
        c["author"]["name"]
        for c in row.get("contributions") or []
        if c.get("author") and (c.get("contribution") or "").strip().lower() in NARRATOR_ROLES
    ]
    if not narrators:
        default_label = ""
    elif len(narrators) > 3:
        default_label = "Full Cast"
    else:
        default_label = " & ".join(narrators)
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "subtitle": row.get("subtitle") or "",
        "format": row.get("edition_format") or "",
        "asin": row.get("asin"),
        "isbn_13": row.get("isbn_13"),
        "audio_seconds": row.get("audio_seconds"),
        "release_date": row.get("release_date"),
        "users_count": row.get("users_count") or 0,
        "publisher": (row.get("publisher") or {}).get("name") or "",
        "narrators": narrators,
        "narrator": ", ".join(narrators),
        "default_label": default_label,
    }
