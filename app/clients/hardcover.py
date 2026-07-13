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
        cached_image
        contributions {
          author { id name }
        }
        book_series {
          position
          featured
          series { id name }
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

    def search_books(
        self, query: str, per_page: int = 25, page: int = 1
    ) -> list[dict[str, Any]]:
        data = self.execute(
            SEARCH_QUERY, {"query": query, "perPage": per_page, "page": page}
        )
        results = (data.get("search") or {}).get("results") or {}
        return [_parse_search_document(hit["document"]) for hit in results.get("hits", [])]


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
    image = doc.get("image") or {}
    return {
        "hardcover_id": int(doc["id"]),
        "title": doc["title"],
        "authors": authors,
        "series_name": (featured.get("series") or {}).get("name"),
        "series_position": featured.get("position"),
        "cover_url": image.get("url"),
        "release_year": doc.get("release_year"),
        "has_audiobook": bool(doc.get("has_audiobook")),
        "users_count": doc.get("users_count") or 0,
    }
