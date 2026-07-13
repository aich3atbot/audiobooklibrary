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
| Import mode | Default **hardlink-or-copy** (leaves the download in place so seeding torrents aren't broken); `IMPORT_MODE=move` relocates instead. Deviation from the original "move" wording, for seeding safety. |
| Read-state sync | **Two-way**: UI changes push to Hardcover immediately; a periodic sync pulls Hardcover changes down. Hardcover is the source of truth for read state. |
| Web stack | **FastAPI + Jinja2 + HTMX** (server-rendered, HTMX for in-page updates). |
| Users | Single user, single Hardcover account. Optional single-user login (`AUTH_USERNAME`/`AUTH_PASSWORD` env vars) with a signed session cookie; when unset the app runs open (trusted LAN / reverse proxy). |
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
HARDCOVER_TOKEN         # bearer token from hardcover.app settings ("Bearer " prefix tolerated)
PROWLARR_URL            # e.g. http://prowlarr:9696
PROWLARR_API_KEY
PROWLARR_CATEGORIES     # default 3030 (Audio/Audiobook), comma-separated
DOWNLOAD_DIR            # default /downloads
LIBRARY_DIR             # default /audiobooks
CONFIG_DIR              # default /config (sqlite db location)
SYNC_INTERVAL_MINUTES   # default 30
WATCH_INTERVAL_SECONDS  # default 30 (download dir poll)
DOWNLOAD_QUIET_SECONDS  # default 120 (download "finished" quiet period)
IMPORT_MODE             # copy (default, hardlink-or-copy) | move
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

## Milestones (all complete)

1. ✅ **Skeleton** — project scaffold, config, DB models/migrations, FastAPI app boots, healthcheck, Dockerfile.
2. ✅ **Hardcover pull** — client + sync task; library page renders synced books with covers and read states.
3. ✅ **Read-state two-way sync** — UI state changes push to Hardcover; retry/pending handling.
4. ✅ **Hardcover search & add** — search page; add books with a chosen state.
5. ✅ **Prowlarr search & grab** — release picker, grab flow, release tracking.
6. ✅ **Watch & import** — download watcher, importer with Audiobookshelf-style renaming, activity page, failure handling.
7. ✅ **Polish & ship** — settings page with connection checks, library filters/sort, docker-compose example, README, test pass.

Each milestone ended in a working, verified state (one commit per milestone).

## Testing

- Unit tests for state mapping, filename sanitization, path templating, release↔download matching.
- Client tests against recorded/mocked HTTP (respx) for Hardcover and Prowlarr.
- An importer integration test using tmp dirs with fake download layouts (single m4b, multi-file mp3, nested folders).

## Collection import (/imports)

A staging volume + review UI for bringing an *existing* audiobook collection into the
library. Complements (does not change) the automatic /downloads pipeline.

- **Volume**: optional, hard-coded `/imports` (no env var — the app always runs in Docker;
  the internal setting exists only so tests can redirect it). The Imports page explains how
  to mount it when the directory is missing.
- **Scanner**: recursive; any folder that *directly* contains audio files is one audiobook
  entry (so `Author/Series/Book/` layouts work). Multi-disc folders (children named like
  `CD1`, `Disc 2`, `Part 3`) are grouped as a single entry. Loose audio files at a scanned
  level are single-file entries. Entry = relative path + audio file count + total size.
- **Matcher**: scores entries against local (Hardcover-synced) books using normalized
  name similarity of the entry folder (with parent folders as author/series hints) vs
  title / author+title. High-confidence matches are pre-selected; low-confidence ones are
  suggestions; below threshold the entry is unmatched. Books already imported
  (`library_path` set) are excluded from matching.
- **Amend**: each row's match can be changed — pick from local-library search, or search
  Hardcover and add-with-state (per-book state dropdown, defaulting to *read*), which
  shelves the book and syncs its metadata, then matches it.
- **Import**: per-row button, bulk-select, or import-all-matched. Import always **moves**
  (regardless of IMPORT_MODE, which remains download-pipeline-only): files go to the
  standard `Author/Series/{index} - Title/` path, the source folder is removed from
  /imports, and now-empty parent folders are cleaned up. Book becomes
  `download_state=imported` with `library_path` set. Destination-exists and other failures
  are reported per row; nothing is guessed.
- **Safety**: import paths are validated to stay inside IMPORTS_DIR; a book that already
  has a `library_path` can't be the target of an import.

## Audiobookshelf-compatible API

Implement enough of the Audiobookshelf server API (https://api.audiobookshelf.org) that ABS
client apps can log in, browse, stream, download, and sync progress against this server.
Primary compatibility target: the **official ABS app** (Android/iOS); third-party clients
(Plappa, ShelfPlayer) should mostly work as a byproduct and get quirk fixes on demand.

Decisions:
- **Auth**: same credentials as the UI. `POST /login` (JSON) issues a signed bearer token
  (same secret as the session cookie); ABS endpoints authenticate via `Authorization:
  Bearer`. The UI's form login and cookie session are unchanged — `/login` dispatches on
  content type. `/status`, `/ping` stay public (server discovery).
- **Streaming**: direct play only — original m4b/mp3 files served with HTTP Range support.
  No ffmpeg/HLS. (ABS apps direct-play these formats natively.)
- **Progress**: stored locally per book (cross-device resume). When a client reports a book
  finished, mark it read on Hardcover with today's date (same code path as the UI toggle).
- **Catalogue**: tracked books only (`library_path` set); one fixed library
  ("Audiobooks"). Covers served from a cover image in the book folder when present, else
  proxied from the Hardcover CDN URL.

New persistence:
- `audio_file` — per-book audio tracks: index, relative path, size, mime, duration
  (+ chapters for m4b), read with **mutagen** at import time, with a startup backfill scan
  for already-imported books.
- `media_progress` — single-user progress per book: current_time, duration, is_finished,
  updated_at. Playback sessions are in-memory; each `/api/session/:id/sync` updates the
  progress row.

Endpoint surface (v1):
- Discovery/auth: `GET /status`, `GET /ping`, `POST /login`, `POST /api/authorize`,
  `GET /api/me`
- Catalogue: `GET /api/libraries`, `/api/libraries/:id`, `/api/libraries/:id/items`
  (pagination + basic sort), `/api/libraries/:id/personalized` (continue-listening /
  recently-added shelves for the app home screen), `/api/libraries/:id/series`,
  `/api/libraries/:id/authors`, `/api/libraries/:id/filterdata`
- Items: `GET /api/items/:id` (expanded, with audioFiles/chapters/tracks),
  `GET /api/items/:id/cover`
- Playback: `POST /api/items/:id/play` (direct-play session), audio file serving with
  Range, `POST /api/session/:id/sync`, `POST /api/session/:id/close`
- Progress/downloads: `GET/PATCH /api/me/progress/:itemId`, per-file download endpoints
  (+ whole-item zip if the app requires it)
- socket.io: not implemented initially; verify the official app degrades gracefully and
  add a minimal shim only if required.

Build order (commit per phase):
1. ✅ **Contract research** — docs/abs-api-contract.md, pinned from ABS server + app source.
2. ✅ **Auth + discovery** — JWTs (access/refresh/legacy), login content negotiation,
   status/ping/authorize/me; middleware split (cookie redirect for UI, 401 JSON for API).
3. ✅ **Audio metadata** — mutagen, `audio_file` + `media_progress` tables, import-time
   scan + startup backfill.
4. ✅ **Catalogue** — libraries/items/series/authors/filterdata/personalized + covers.
5. ✅ **Playback + progress** — direct-play sessions, Range streaming, per-file downloads,
   sync/close + local-session sync, finished→Hardcover, socket.io shim
   (entrypoint is now `app.main:asgi`).
6. **Device verification (pending)** — user tests with the real app against the
   container; iterate on quirks.

Testing: unit tests generate tiny valid silent MP3s programmatically so mutagen scanning
and Range serving are covered without binary fixtures; endpoint shapes asserted against
the researched contracts. Final acceptance is the real app on a real device (phase 6),
which only the user can perform.

## Future work (out of scope for this build)

- Public REST API: list books, download audio files, update reading state — designed to serve
  the companion app below (the internal routes should keep this in mind but v1 does not expose it).
- Android/iOS audiobook player companion app (similar to Smart Audiobook Player) that
  downloads from the library, plays audiobooks, and updates read status.
