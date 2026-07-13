# Audiobook Library — Build Plan

A self-hosted audiobook manager: syncs your book list from Hardcover, finds audiobook
releases via Prowlarr, tracks downloads, and organizes finished audiobooks into a clean
library folder. Single container, Python, SQLite.

## Goals (from original brief)

- Integrate with the Hardcover API (https://hardcover.app/) to retrieve a user's book list,
  respecting Hardcover's states: *want to read*, *reading*, *read* (including read date).
- Search for books using Prowlarr's API.
- Monitor a download directory for finished downloads.
- Move downloaded books into an audiobooks folder, renaming as necessary.
- Web UI to list books (author, title, series, cover art, read state, downloaded state),
  update read state, and search Hardcover for new books to track and download.

## Decisions

| Topic | Decision |
|---|---|
| Download flow | App triggers a **grab via Prowlarr**; Prowlarr hands the release to its configured download client. The app watches the download directory for the finished files. |
| Library layout | **Audiobookshelf-style**: `Author/Series/{SeriesIndex} - Title/` (no series: `Author/Title/`). |
| Read-state sync | **Two-way**: UI changes push to Hardcover immediately; a periodic sync pulls Hardcover changes down. Hardcover is the source of truth for read state. |
| Web stack | **FastAPI + Jinja2 + HTMX** (server-rendered, HTMX for in-page updates). |
| Users | Single user, single Hardcover account. No app-level auth in v1 (assumed to run behind a reverse proxy / on a trusted LAN). |
| Configuration | Environment variables (12-factor), with a `.env` file for local dev. |

## Architecture

One FastAPI process running:

- **Web UI** — Jinja2 templates + HTMX partials, styled with a lightweight CSS framework (Pico.css or similar).
- **Background workers** — asyncio tasks in the same process (no Celery/Redis needed at this scale):
  - *Hardcover sync* — periodic pull of the user's library (default every 30 min, configurable).
  - *Download watcher* — polls the download directory for completed downloads matched to grabbed releases.
  - *Importer* — moves/renames completed audiobooks into the library folder.
- **SQLite** via SQLAlchemy 2.x + Alembic migrations. DB file lives on a mounted volume.

```
┌────────────────────────── container ──────────────────────────┐
│  FastAPI app                                                  │
│  ├─ UI routes (Jinja/HTMX)                                    │
│  ├─ Hardcover client (GraphQL, bearer token)                  │
│  ├─ Prowlarr client (REST, API key)                           │
│  └─ background tasks: sync ▸ watch ▸ import                   │
│                                                               │
│  volumes:  /config (sqlite db)                                │
│            /downloads (watched dir, shared with dl client)    │
│            /audiobooks (organized library)                    │
└───────────────────────────────────────────────────────────────┘
```

## Data model (SQLite)

- **author** — id, hardcover_id, name
- **series** — id, hardcover_id, name
- **book** — id, hardcover_id, title, author_id, series_id (nullable), series_index (nullable),
  cover_url, read_state (`want_to_read` | `reading` | `read` | `none`), read_at (date, nullable),
  download_state (`none` | `wanted` | `grabbed` | `downloading` | `downloaded` | `imported` | `failed`),
  library_path (nullable), created_at, updated_at
- **release** — id, book_id, prowlarr_guid, indexer_id, title, size, seeders, grabbed_at, status
  (tracks what we asked Prowlarr to grab, used to match finished downloads)
- **settings/state** — key-value table for sync cursors (e.g. last Hardcover sync time)

A book's identity is anchored on `hardcover_id` (specifically the Hardcover *book* id; editions
are collapsed to the canonical book).

## External integrations

### Hardcover (GraphQL, `https://api.hardcover.app/v1/graphql`, bearer token)

- **Pull**: query the authenticated user's `user_books` with status, dates, book, contributors
  (author), series, and cover image. Map Hardcover status ids → our read states.
- **Push**: mutations to insert/update `user_books` when read state changes in the UI
  (including setting the read date when marking *read*).
- **Search**: Hardcover's search API for the UI's "add book" search (by title/author/series).
- Rate limiting: modest request rate, cache search results briefly; sync is incremental where
  possible (updated-since cursor), full refresh as fallback.
- *First implementation task: verify current schema/field names against the live API with the
  user's token — the Hardcover API is beta and shifts occasionally.*

### Prowlarr (REST, API key)

- **Search**: `GET /api/v1/search?query=...&categories=3030` (Audio/Audiobook category),
  optionally filtered to configured indexers.
- **Grab**: `POST /api/v1/search` with the release `guid` + `indexerId` — Prowlarr forwards it
  to its configured download client.
- The app records the grabbed release title/size so the download watcher can match the
  resulting folder/files in `/downloads`.

## Core workflows

1. **Hardcover sync (periodic + manual "Sync now")**
   Pull user library → upsert authors/series/books → update read states/dates locally.
   Local-only unsynced changes are pushed first, then pull (Hardcover wins conflicts).

2. **Want → download**
   User marks a book *wanted* (or downloads directly from search results) → app searches
   Prowlarr using `"{author} {title}"` (fallback: title only) → user picks a release from
   results (with a "best match" suggestion by seeders/size) → app grabs via Prowlarr →
   `download_state = grabbed`.

3. **Download watch → import**
   Watcher polls `/downloads` for entries matching grabbed release names, waits for the
   download to be complete/stable (size unchanged across polls, no `.part`/incomplete
   markers) → importer moves audio files (m4b/m4a/mp3/flac/ogg + cover/nfo) into
   `/audiobooks/Author/Series/{index} - Title/`, sanitizing filesystem-unsafe characters →
   `download_state = imported`, `library_path` set. Failures flag the book for manual
   review in the UI rather than guessing.

4. **Read-state update**
   UI toggle → optimistic local update → push mutation to Hardcover → on failure, mark
   pending and retry on next sync.

## UI (server-rendered + HTMX)

- **Library page** (`/`) — grid/list of books with cover art, author, series (+index), read-state
  badge, download-state badge. Filters: read state, download state; text filter; sort by
  author/title/recent. Inline actions: change read state, search/download.
- **Search page** (`/search`) — Hardcover search by title/author/series; results show cover +
  metadata; actions: *add with state* (want to read / reading / read) and *find downloads*.
- **Release picker** (modal/partial) — Prowlarr results for a book: title, size, indexer,
  seeders; click to grab.
- **Activity page** (`/activity`) — grabbed/downloading/importing items, recent imports,
  failures needing attention (with retry / manual-match actions).
- **Settings page** (`/settings`) — connection status for Hardcover & Prowlarr, sync interval,
  paths (read-only display of env config), "Sync now" button.

## Configuration (env vars)

```
HARDCOVER_TOKEN        # bearer token from hardcover.app settings
PROWLARR_URL           # e.g. http://prowlarr:9696
PROWLARR_API_KEY
DOWNLOAD_DIR           # default /downloads
LIBRARY_DIR            # default /audiobooks
CONFIG_DIR             # default /config (sqlite db location)
SYNC_INTERVAL_MINUTES  # default 30
```

## Container

- Single `Dockerfile` (python:3.12-slim, uv or pip install, uvicorn entrypoint).
- `docker-compose.yml` example wiring volumes and env vars (Prowlarr/download client are
  external, run by the user).
- Healthcheck endpoint (`/healthz`).

## Project layout

```
audiobooklibrary/
├─ app/
│  ├─ main.py               # FastAPI app, lifespan starts background tasks
│  ├─ config.py             # pydantic-settings
│  ├─ db.py, models.py      # SQLAlchemy + models
│  ├─ clients/hardcover.py  # GraphQL client
│  ├─ clients/prowlarr.py   # REST client
│  ├─ services/sync.py      # Hardcover sync logic
│  ├─ services/downloads.py # grab, watch, import
│  ├─ routes/               # ui.py, books.py, search.py, activity.py, settings.py
│  ├─ templates/            # Jinja2 + HTMX partials
│  └─ static/
├─ alembic/                 # migrations
├─ tests/                   # pytest; clients mocked with respx
├─ Dockerfile
├─ docker-compose.yml
└─ pyproject.toml
```

## Milestones

1. **Skeleton** — project scaffold, config, DB models/migrations, FastAPI app boots, healthcheck, Dockerfile.
2. **Hardcover pull** — client + sync task; library page renders synced books with covers and read states.
3. **Read-state two-way sync** — UI state changes push to Hardcover; retry/pending handling.
4. **Hardcover search & add** — search page; add books with a chosen state.
5. **Prowlarr search & grab** — release picker, grab flow, release tracking.
6. **Watch & import** — download watcher, importer with Audiobookshelf-style renaming, activity page, failure handling.
7. **Polish & ship** — settings page, docker-compose example, README, test pass.

Each milestone ends in a working, testable state.

## Testing

- Unit tests for state mapping, filename sanitization, path templating, release↔download matching.
- Client tests against recorded/mocked HTTP (respx) for Hardcover and Prowlarr.
- An importer integration test using tmp dirs with fake download layouts (single m4b, multi-file mp3, nested folders).

## Future work (out of scope for this build)

- Public REST API: list books, download audio files, update reading state — designed to serve
  the companion app below (the internal routes should keep this in mind but v1 does not expose it).
- Android/iOS audiobook player companion app (similar to Smart Audiobook Player) that
  downloads from the library, plays audiobooks, and updates read status.
