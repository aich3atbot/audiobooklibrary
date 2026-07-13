# Audiobook Library

Self-hosted audiobook manager: syncs the user's book list from Hardcover, finds audiobook
releases on a torrent indexer (AudioBookBay), downloads them with a torrent client
(Deluge), and organizes finished audiobooks into an Audiobookshelf-style library folder.

**`plan.md` is the authoritative spec** — read it before making changes. It contains the full
architecture, data model, workflows, and milestones. Keep it updated as decisions change.

## Current state

All seven plan.md milestones plus auth, collection import (/imports), an
Audiobookshelf-compatible API, and the direct torrent pipeline (AudioBookBay + Deluge,
which replaced Prowlarr) are built, tested, and committed. Run `uv run pytest` (all
external APIs are mocked with respx). Two things await real-world verification: the ABS
API with the official app (plan.md phase 6), and one real UI grab → download → import
through the user's own Deluge.

## ABS-compatible API

Lives in `app/abs/`. **docs/abs-api-contract.md pins the exact protocol shapes, verified
from the ABS server/app source — do not code ABS endpoints from memory; check the doc,
and re-verify against source when extending.** JWTs share the UI session secret; /login
dispatches JSON (ABS) vs form (UI) by content type. The served entrypoint is
`app.main:asgi` (FastAPI wrapped in a socket.io shim), not `app.main:app`. Only books
with `library_path` set are exposed. ABS logins are user accounts (the virtual admin is
rejected); token `userId` is the account's stable uuid. Finishing a book in an app marks
it read on Hardcover via `update_read_state`.

## Key decisions (do not silently revisit)

- **Stack**: Python 3.12, FastAPI + Jinja2 + HTMX (server-rendered, no SPA), SQLAlchemy 2.x +
  Alembic on SQLite, single container, uvicorn.
- **Background work**: asyncio tasks inside the FastAPI process (lifespan-managed). No
  Celery/Redis — do not introduce them.
- **Downloads**: the app searches the indexer itself, resolves the chosen release's details
  page to a magnet, and adds it to the torrent client. Both sides sit behind protocols
  (`app/clients/indexer.py`, `app/clients/download_client.py`) — add new indexers/clients
  there, don't special-case. **Prowlarr is gone; do not reintroduce it.**
- **Tracking downloads**: the watcher polls the client by `Release.info_hash` for progress
  and completion; the old name-matching directory watch is the fallback (null hash, torrent
  gone from the client, client unreachable). Keep both — a client that is down must never
  block an import.
- **Cancel deletes.** Cancelling a release removes the torrent *and its data* from the
  download client (`remove_torrent(hash, remove_data=True)`), ending any seeding of it —
  the user chose this explicitly over preserving seeds. The UI confirms first. If the
  client can't be reached, cancel still succeeds locally (a dead client must not trap the
  release) but records on `release.error` that the torrent may still be running.
- **Library layout**: `Author/Series/{SeriesIndex} - Title/`, or `Author/Title/` when there is
  no series. Sanitize filesystem-unsafe characters.
- **Import mode**: default is hardlink-or-copy (seeding torrents keep their files);
  `IMPORT_MODE=move` opts into relocating. Do not change the default back to move.
- **Collection import** (`/imports` volume, Imports page): hard-coded path, no env var
  (`Settings.imports_dir` exists only for tests). Always MOVES files (draining /imports is
  the point) regardless of IMPORT_MODE, which applies to the download pipeline only.
- **Read state**: per user (`user_book` rows over shared `book` metadata), two-way sync with
  each user's own Hardcover token, but **Hardcover is the source of truth** — push local
  changes first, then pull; Hardcover wins conflicts. Book identity is anchored on the
  Hardcover *book* id (editions collapsed). Download state stays shared on `book`
  (displayed as not present / downloading / available; a book one user made available
  cannot be grabbed again — search offers "add to my library" instead).
- **Multi-user, mandatory auth**: there is no open mode. Users are DB rows (scrypt
  password hashes via `app/passwords.py`, per-user Hardcover tokens); the virtual `admin`
  account (password from `ADMIN_PASSWORD`, required at startup, reserved username) sees
  only the user-administration UI at /admin/users. Disabling a user locks them out
  immediately — sessions and ABS tokens are re-checked against the DB per request. Auth
  lives in `app/auth.py` (middleware + deps) + `app/routes/auth.py`; admin routes in
  `app/routes/admin.py`. The multi-user conversion is in progress — see plan.md
  "Multi-user conversion" for design and remaining milestones.
- **Config via env vars** only — see `.env.example` for the full list (auth, Hardcover,
  indexer, download client, paths, intervals, import mode). `DOWNLOAD_DIR` must be the
  directory the torrent client writes *completed* downloads to. The session-cookie secret
  is not configurable; it is auto-generated and persisted at `CONFIG_DIR/session_secret`.

## Sandbox environment

Agents work on this project inside a Docker sandbox (`audiobooklibrary-sbx`) with a
default-deny network policy. Blocked HTTP requests return a 403 with a
`Blocked by network policy` body — that means the sandbox policy, not the remote service,
is the problem. When a needed service is unreachable, **prompt the user to allow it** with
`sbx policy allow network <domain>` on the host, then retest. The host's Deluge is reached
at `http://host.docker.internal:8112` from inside the sandbox, never as `localhost`.
`api.hardcover.app`, `audiobookbay.fi` and the host's Deluge are all confirmed reachable
with the current policy.

## External API gotchas

- **Hardcover** (`https://api.hardcover.app/v1/graphql`, bearer token): the API is beta and
  the schema shifts. Verify field/query names against the live API (introspection with the
  user's token) before writing or changing queries — do not trust remembered schema.
- **AudioBookBay** (HTML scraping, no API — contract pinned in plan.md, verified live):
  a **browser User-Agent is mandatory** (ABB blocks tool UAs); an exhausted search page
  returns 200 with zero posts, not a 404; post metadata sits in inline `<span>`s split by
  `<br>`, so parse element *text*, not raw HTML; some mirrors base64-encode posts
  (`div.post.re-ab`). No seeder counts exist — don't invent a "best match" ranking. Mirrors
  rotate domains and some have expired TLS certs, so `INDEX_URL` may be plain `http://`.
- **Deluge** (web UI JSON-RPC at `{DOWNLOAD_URL}/json`, verified against 2.2.0): auth is
  `auth.login([password])`, password-only — an **empty password is valid**, so never gate a
  connection check on it. **`is_finished` is the completion signal, never the `state`
  string** (finished torrents commonly sit in state "Queued"). `save_path` is in Deluge's
  filesystem namespace, so find downloads by the torrent's `name` inside `DOWNLOAD_DIR`.
  `daemon.info` does not exist on 2.2.0 — use `daemon.get_version`.

## Conventions

- Project layout, data model, and state enums (`read_state`, `download_state`) are defined in
  plan.md — follow them exactly so UI, sync, and importer stay consistent.
- Import failures must surface in the Activity page for manual review — never guess a match.
- Tests: pytest; mock HTTP with respx; importer tests use tmp dirs with fake download layouts.
- Each milestone must end in a working, runnable state.
