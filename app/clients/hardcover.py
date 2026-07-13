"""Hardcover GraphQL API client.

The API is beta and its schema shifts; the queries below were verified against
the live API. `me` returns a list of users, not a single object.
"""

from typing import Any

import httpx

API_URL = "https://api.hardcover.app/v1/graphql"
PAGE_SIZE = 100

USER_BOOKS_QUERY = """
query UserBooks($limit: Int!, $offset: Int!) {
  me {
    user_books(limit: $limit, offset: $offset, order_by: {id: asc}) {
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
    }
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
