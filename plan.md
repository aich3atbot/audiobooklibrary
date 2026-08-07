# Audiobook Library — Build Plan

A self-hosted audiobook manager: syncs your book list from Hardcover, finds audiobook
releases on a torrent indexer, downloads them through a torrent client, and organizes
finished audiobooks into a clean library folder. Single container, Python, SQLite.

## Goals (from original brief)

- Integrate with the Hardcover API (https://hardcover.app/) to retrieve a user's book list,
  respecting Hardcover's states: *want to read*, *reading*, *read* (including read date).
- Search a torrent indexer (AudioBookBay) for audiobook releases and download them with a
  torrent client (Deluge). *(Originally built on Prowlarr; replaced by direct indexer +
  client control so the app owns the whole pipeline and can report real progress.)*
- Monitor a download directory for finished downloads.
- Move downloaded books into an audiobooks folder, renaming as necessary.
- Web UI to list books (author, title, series, cover art, read state, downloaded state),
  update read state, and search Hardcover for new books to track and download.

## Decisions

| Topic | Decision |
|---|---|
| Download flow | App searches a **torrent indexer directly** (AudioBookBay), resolves the chosen release to a **magnet**, and adds it to a **torrent client** (Deluge) itself. It then polls the client **by info hash** for progress/completion, and imports from the download directory. Directory name-matching remains the fallback when the client can't answer. |
| Indexer / client | Both behind small protocols (`Indexer`, `DownloadClient`) so more can be added. Today: AudioBookBay + Deluge or qBittorrent. |
| Cancelling | Cancel **removes the torrent and deletes its data** in the client, ending any seeding of it, and stops tracking the release here. Chosen deliberately over preserving seeds. The UI asks for confirmation; if the client is unreachable the release is still cancelled locally, with the failure recorded so the user knows the torrent may still be running. |
| Library layout | **Audiobookshelf-style**: `Author/Series/{SeriesIndex} - Title/` (no series: `Author/Title/`). A labelled edition suffixes the **series** folder (`Author/Series {Label}/{idx} - Title/`) or, standalone, the book folder (`Author/Title {Label}/`) — see "Multi-edition support". |
| Import mode | Default **hardlink-or-copy** (leaves the download in place so seeding torrents aren't broken); `IMPORT_MODE=move` relocates instead. Deviation from the original "move" wording, for seeding safety. |
| Read-state sync | **Two-way**: UI changes push to Hardcover immediately; a periodic sync pulls Hardcover changes down. Hardcover is the source of truth for read state. |
| Web stack | **FastAPI + Jinja2 + HTMX** (server-rendered, HTMX for in-page updates). |
| Users | **Multi-user, mandatory login** (signed session cookie). A virtual `admin` account (password from `ADMIN_PASSWORD`, required at startup) only administers users: add, enable/disable, delete, change passwords/tokens. Regular users are DB rows (scrypt password hashes) created by the admin, each with their own Hardcover token. An account is **full** (web UI + Hardcover) or **limited** (Audiobookshelf apps only — see "Limited accounts" below). "admin" is a reserved username. See "Multi-user conversion" below for the full design. |
| Configuration | Environment variables (12-factor), with a `.env` file for local dev. |

## Architecture

One FastAPI process running:

- **Web UI** — Jinja2 templates + HTMX partials, styled with a lightweight CSS framework (Pico.css or similar).
- **Background workers** — asyncio tasks in the same process (no Celery/Redis needed at this scale):
  - *Hardcover sync* — periodic pull of the user's library (default every 30 min, configurable).
  - *Download watcher* — polls the torrent client (by info hash) for progress and completion, falling back to polling the download directory.
  - *Importer* — moves/renames completed audiobooks into the library folder.
- **SQLite** via SQLAlchemy 2.x + Alembic migrations. DB file lives on a mounted volume.

```
┌────────────────────────── container ──────────────────────────┐
│  FastAPI app                                                  │
│  ├─ UI routes (Jinja/HTMX)                                    │
│  ├─ Hardcover client (GraphQL, bearer token)                  │
│  ├─ Indexer client (AudioBookBay, HTML scraping)              │
│  ├─ Download client (Deluge, web UI JSON-RPC)                 │
│  └─ background tasks: sync ▸ watch ▸ import                   │
│                                                               │
│  volumes:  /config (sqlite db)                                │
│            /downloads (dl client's completed-downloads dir)   │
│            /audiobooks (organized library)                    │
└───────────────────────────────────────────────────────────────┘
```

## Data model (SQLite)

- **user** — id, uuid (ABS userId), username (unique), password_hash (scrypt), hardcover_token,
  role (`full` | `limited` — see "Limited accounts"), enabled, created_at, last_sync_at,
  last_sync_result. The `admin` account is virtual — never a row.
- **author** — id, hardcover_id, name, image_url (Hardcover's author photo; NULL = never
  looked up, "" = Hardcover has none)
- **series** — id, hardcover_id, name, hardcover_slug
- **book** — shared metadata only: id, hardcover_id, hardcover_slug, title, author_id,
  series_id (nullable), series_index (nullable), cover_url, created_at, updated_at.
  `hardcover_slug` (both tables) is what hardcover.app urls route on — same three
  states as author.image_url, filled by sync and topped up by a backfill.
  A book may belong to no user's library. Its UI download status is the aggregate over
  its editions (`book_status`: available if any edition has files, else downloading if
  any edition is in the pipeline, else not present).
- **edition** — one audiobook recording of a book (unique book_id+label): book_id,
  hardcover_edition_id (nullable — set when picked from Hardcover's editions),
  label (the grouping name used in folder names, e.g. "Stephen Fry"; `""` is the
  unlabelled edition), narrator (display string), download_state
  (`none` | `grabbed` | `downloading` | `imported` | `failed`; displayed as
  *not present / downloading / available*), library_path (nullable), timestamps.
  Owns the on-disk files and the download pipeline state — see "Multi-edition support".
- **user_book** — one user's shelf membership for a book (unique user_id+book_id):
  hardcover_user_book_id, pending_push, read_state (`want_to_read` | `reading` | `read` | `none`),
  read_at (date, nullable), started_at (date, nullable — Hardcover's
  `first_started_reading_date`), timestamps. Read state stays book-level.
- **release** — id, edition_id, user_id (who grabbed it, nullable), guid (the release's
  details-page URL), indexer, title, size, info_hash, magnet_uri, progress, grabbed_at, status
  (tracks what we handed the torrent client; `info_hash` is how we ask about it later.
  Rows grabbed before the direct-torrent rewrite have a null `info_hash` and fall back to
  name matching.)
- **settings/state** — key-value table for app state (sync cursors live on `user` now)

A book's identity is anchored on `hardcover_id` (specifically the Hardcover *book* id;
sync and shelving collapse editions to the canonical book). Downloaded recordings are
tracked as `edition` rows, optionally tied to a Hardcover *edition* id.

## External integrations

### Hardcover (GraphQL, `https://api.hardcover.app/v1/graphql`, bearer token)

- **Pull**: query the authenticated user's `user_books` with status, dates, book, contributors
  (author), series, and cover image. Map Hardcover status ids → our read states.
- **Push**: mutations to insert/update `user_books` when read state changes in the UI
  (including setting the read date when marking *read*).
- **Search**: Hardcover's search API for the UI's "add book" search (by title/author/series).
  Search documents carry `featured_series.series.id`, which links results to the series page.
- **Series**: `book_series` filtered by `series_id` with `book: {canonical_id: {_is_null: true}}`
  (a series row exists per edition/boxset — HP has 112 rows for 7 books); order by position
  then `users_count desc` and keep the first book per position. Verified live 2026-07.
- **Editions** (verified live 2026-07-18 against Chamber of Secrets, book_id 429306):
  `editions(where: {book_id: {_eq: $id}, reading_format_id: {_eq: 2}}, order_by:
  {users_count: desc})` — **`reading_format_id` 2 ("Listened") is the audiobook format**
  (1 Read, 3 Both, 4 Ebook). Fields confirmed: `id title subtitle edition_format
  edition_information asin isbn_13 audio_seconds release_date users_count
  publisher { name } contributions { contribution author { id name } }`.
  **Narrator extraction must be tolerant**: the `contribution` role string is usually
  `"Narrator"` but appears as `"narrator"`, `"Reader"`, `"Sprecher"`, and is `null` for
  the author — match case-insensitively on narrator/reader. Books can carry dozens of
  audio editions (CoS has 38, mostly foreign/junk) so the picker relies on the
  `users_count desc` ordering; full-cast recordings list 10+ narrators, so the default
  label falls back to "Full Cast" when more than 3 narrators are credited.
- Rate limiting: modest request rate, cache search results briefly; sync is incremental where
  possible (updated-since cursor), full refresh as fallback.
- *First implementation task: verify current schema/field names against the live API with the
  user's token — the Hardcover API is beta and shifts occasionally.*

### AudioBookBay (HTML scraping — there is no API)

Verified against the live site; re-verify before changing the parser.

- **Browser User-Agent is mandatory** — ABB blocks tool UAs (this is what the
  `prowlarr-abb` fork existed to work around).
- **Search**: `GET /?s={term}&tt=1`, term lowercased with non-word characters collapsed to
  spaces; page N is `/page/{N}/`. An exhausted page returns **HTTP 200 with zero posts**,
  not a 404, so paging stops on "no posts parsed".
- **Results**: `div.post` containing `div.postTitle`; title/link from `div.postTitle h2 a`
  (href is relative). Format / Bitrate / File Size / Posted live in text lines separated by
  `<br>` with the values inside inline `<span>`s — parse the element's *text*, not raw HTML.
  Some mirrors base64-encode post bodies (`div.post.re-ab`); decode before parsing.
- **No seeder counts exist**, so results keep the site's own order — the release picker
  shows size/format/date instead of a fabricated "best match".
- **Grab**: the details page carries the torrent's info hash
  (`<td>Info Hash:</td><td>{40-hex}</td>`) and its tracker list (`Announce URL:` /
  `Tracker:` rows). We build a magnet from those (public trackers as fallback).
- Mirrors rotate domains and some serve expired TLS certs, hence `INDEX_URL` is
  user-configured and may legitimately be plain `http://`.

### Deluge (web UI JSON-RPC, `POST {DOWNLOAD_URL}/json`)

Verified against Deluge WebUI 2.2.0.

- **Auth** is `auth.login([password])` — password-only, and an empty password is valid.
  `DOWNLOAD_USERNAME` is accepted but unused (reserved for clients that need one). Login
  sets a session cookie; an expired session answers "Not authenticated" (code 1), so calls
  re-login once and retry.
- The web process connects to the daemon separately: if `web.connected()` is false,
  `web.connect()` to the first `web.get_hosts()` host.
- **Add**: `core.add_torrent_magnet(uri, {})` → info hash. A torrent Deluge already has is
  a success, not an error (the hash is recovered from the magnet).
- **Poll**: `core.get_torrents_status({"id": [hashes]}, [...])`. **`is_finished` is the
  completion signal — never the state string**: a finished torrent commonly sits in state
  "Queued". `save_path` is in *Deluge's* filesystem namespace, so the importer locates the
  download by the torrent's `name` inside `DOWNLOAD_DIR`, not by that path.
- `daemon.info` does **not** exist on 2.2.0; the version comes from `daemon.get_version`.

### qBittorrent (Web UI API, `{DOWNLOAD_URL}/api/v2/...`)

Verified live against qBittorrent 5.2.3 / Web API 2.15.1.

- **Auth**: `POST auth/login` (form username+password) answers **204** (docs say 200
  "Ok."); bad credentials answer 401 (pre-5.x: 200 "Fails."). The session is the SID
  cookie; an expired session answers **403** on any call, so calls re-login once and retry.
- **Add**: `POST torrents/add` (form `urls=`) answers JSON with `added_torrent_ids`
  (pre-5.x: bare "Ok." → hash parsed from the magnet). A duplicate add answers **409** —
  treated as success. Passing `category=` **auto-creates the category**; `DOWNLOAD_LABEL`
  maps to a category (the Sonarr/Radarr convention).
- **Poll**: `GET torrents/info?hashes=h1|h2`. `progress` is **0..1** (scaled to 0..100).
  **`amount_left` is 0 for a metadata-less torrent, so completion is `progress >= 1.0`**,
  never `amount_left == 0`. `total_size` is -1 before metadata (clamped to 0). Unknown
  hashes are simply absent. `save_path` is in qBittorrent's namespace, same caveat as Deluge.
- **Remove**: `POST torrents/delete` (form `hashes`, `deleteFiles`) answers 200 even for
  hashes it doesn't have.

## Core workflows

1. **Hardcover sync (periodic + manual "Sync now")**
   Pull user library → upsert authors/series/books → update read states/dates locally.
   Local-only unsynced changes are pushed first, then pull (Hardcover wins conflicts).

2. **Want → download**
   User marks a book *wanted* (or downloads directly from search results) → app searches the
   indexer using `"{author} {title}"` (fallback: title only) → user picks a release (size /
   format / posted date; the indexer's own ordering), optionally choosing which edition it
   is (Hardcover editions picker / free label; default: the unlabelled edition) → app reads
   the release's details page for the info hash, builds a magnet, and adds it to the
   torrent client → the edition's `download_state = grabbed`, `info_hash` recorded.

3. **Download watch → import**
   Watcher asks the torrent client about each active release's `info_hash`: it records
   `progress` (shown on Activity) and, when the client reports the torrent **finished**,
   imports immediately — the client is authoritative, so the quiet-period and
   incomplete-marker heuristics are skipped. The download is located by the torrent's own
   `name` in `/downloads` (its `save_path` is in the client's namespace, not ours); if it
   isn't there, the release **fails loudly** pointing at the volume mapping rather than
   spinning forever.
   *Fallback* (no hash, torrent gone from the client, or client unreachable): the original
   behavior — match `/downloads` entries by release name and wait for the download to be
   complete/stable (nothing changed for `DOWNLOAD_QUIET_SECONDS`, no `.part`/incomplete
   markers). A download client that is down costs progress reporting, never an import.
   Importer then places audio files (m4b/m4a/mp4/mp3/flac/ogg/opus/aac/wma + cover/nfo)
   into the release's edition folder — `/audiobooks/Author/Series/{index} - Title/`, with the
   edition label suffixed per "Multi-edition support" — sanitizing filesystem-unsafe
   characters → the edition's `download_state = imported`, `library_path` set.
   Releases lie about extensions, so each audio candidate is identified by its *contents*
   (`app/services/audio_format.py`, header-only — ~35ms on a 1.2GB file). Identification
   only ever **rules a file out**; it never gatekeeps one that would otherwise import, so
   a file mutagen cannot parse is still placed under its own name (an unreadable header
   costs metadata, not the audiobook). Two things are dropped: a positively-identified
   video track, and a `.mp4` we cannot confirm holds audio — `.mp4` names a video
   container as readily as an audio one. A file whose contents contradict its extension
   is **renamed** to the format it really is; extensions are grouped into families
   (`.m4b`/`.m4a`/`.mp4` are all MP4) so a rename only fires on a genuine mismatch. With `DOWNLOAD_REMOVE_IMMEDIATELY=true`
   the torrent (and its data) is then removed from the client — otherwise it keeps seeding
   per the client's settings; a failed removal never un-imports, it is noted on the
   release's Activity row. Failures flag the book for manual
   review in the UI rather than guessing.

4. **Replace an available edition's files**
   Clicking a library card's cover art, title, or *available* badge opens the book
   detail page (`/books/{id}`) — per edition: label, library path, a rename control,
   and a lazily loaded file table (size and mutagen bitrate) behind a disclosure on
   the path — with per-edition "Replace…" actions (plus "Download another edition…" —
   see "Multi-edition support"). Grabs launched from the detail page answer with an
   `HX-Redirect` back to it (there is no card to OOB-swap there).
   The replace picker is the normal release picker plus a radio choice: remove the
   current files **after the new download imports** (default — the deferred intent is an
   `app_state` key `replace:{release_id}`, consumed by the importer, which clears the old
   `library_path` dir before placing the new files) or **immediately** (files deleted at
   grab time; the edition drops out of the ABS API until the new import lands). The
   replacement lands on the same edition row; siblings are untouched. Replacing touches
   library files only — the old torrent is never removed from the client. A failed
   replacement import whose old files survived leaves the edition available; cancelling
   the replacing release restores *available* when `library_path` is intact.

5. **Read-state update**
   UI toggle → optimistic local update → push mutation to Hardcover → on failure, mark
   pending and retry on next sync.

## UI (server-rendered + HTMX)

- **Library page** (`/`) — grid/list of books with cover art, author, series (+index), read-state
  badge, download-state badge. Filters: read state, download state; text filter; sort by
  author/title/recent. Inline actions: change read state, search/download. Cover art,
  title and the *available* badge link to the book detail page.
- **Book detail page** (`/books/{id}`) — any book, any status: metadata plus every
  edition (label, path, rename/replace, lazily loaded file table; in-flight editions
  show their download state) and the download / download-another-edition entries. See
  workflow 4 and "Multi-edition support".
- **Search page** (`/search`) — Hardcover search by title/author/series; results show cover +
  metadata; actions: *add with state* (want to read / reading / read) and *find downloads*.
- **Series page** (`/series/{hardcover_series_id}`) — the full series fetched live from
  Hardcover, merged with local state: books on the user's shelf render as normal library
  cards (read state, download), the rest as addable search results with a Download button.
  Downloading an unshelved book auto-shelves it as *want to read* first (downloading implies
  wanting it; the shelf is the source of truth), then opens the release picker. Series names
  on library cards and search results link here.
- **Release picker** (modal/partial) — indexer results for a book: title, size, format,
  posted date; click to grab.
- **Activity page** (`/activity`) — grabbed/downloading/importing items with the torrent
  client's progress percentage, recent imports, failures needing attention (with retry /
  manual-match actions). Rows show the edition label being downloaded (when set) and
  book titles link to the book detail page. *Cancel & delete* removes the torrent and
  its data from the client (confirmation prompt first), then stops tracking the release.
- **Settings page** (`/settings`) — connection status for Hardcover, the indexer and the
  download client, sync interval, paths (read-only display of env config), "Sync now" button.

## Configuration (env vars)

The defaults below are applied by `app/config.py`, which is the only place they are
written down: `docker-compose.yml` lists the optional variables bare so an unset one is
never passed into the container, and a blank value is treated as absent (except where the
default is itself empty — there empty is a real setting). Everything but `ADMIN_PASSWORD`
and the four bind-mount paths can simply be left out.

```
ADMIN_PASSWORD          # password for the virtual "admin" account (required at startup)
                        # (Hardcover tokens are per-user, set on the admin's Users page)
INDEX_URL               # AudioBookBay base URL (mirrors rotate; http:// may be the working one)
DOWNLOAD_CLIENT         # "deluge" or "qbittorrent"; optional — downloading is enabled
                        # only when both this and DOWNLOAD_URL are set; leaving either
                        # empty disables it (download UI hidden, DOWNLOAD_* below not
                        # needed; manual import and the rest keep working)
DOWNLOAD_URL            # the client's *web UI*, e.g. http://host.docker.internal:8112
DOWNLOAD_USERNAME       # required by qBittorrent; unused by Deluge (password-only auth)
DOWNLOAD_PASSWORD       # may legitimately be empty (Deluge)
DOWNLOAD_LABEL          # optional label/category for the app's torrents; empty = none
DOWNLOAD_REMOVE_IMMEDIATELY  # default false — true removes torrent+data after import (no seeding)
DOWNLOAD_DIR            # default /downloads — the client's *completed*-downloads directory
LIBRARY_DIR             # default /audiobooks
CONFIG_DIR              # default /config (sqlite db location)
SYNC_INTERVAL_MINUTES   # default 30
WATCH_INTERVAL_SECONDS  # default 30 (download dir poll)
DOWNLOAD_QUIET_SECONDS  # default 120 (download "finished" quiet period)
IMPORT_MODE             # copy (default, hardlink-or-copy) | move
PUID / PGID             # compose-only, optional (default 1000:1000): the uid:gid the
                        # container runs as (docker-compose `user:`), owning written files
```

## Container

- Single `Dockerfile` (python:3.12-slim, uv or pip install, uvicorn entrypoint).
- `docker-compose.yml` example wiring volumes and env vars (the torrent client is external,
  run by the user).
- **Non-root**: compose sets `user: "${PUID:-1000}:${PGID:-1000}"`, covering the whole
  CMD (alembic + uvicorn), so written files are owned by that user. No `USER` in the
  image and no entrypoint/gosu privilege handling — the uid is operator-configurable
  without a rebuild, and mount ownership is the operator's responsibility (a one-time
  `chown` when upgrading from the root-running versions; see README).
- Healthcheck endpoint (`/healthz`).

## Project layout

```
audiobooklibrary/
├─ app/
│  ├─ main.py               # FastAPI app, lifespan starts background tasks
│  ├─ config.py             # pydantic-settings
│  ├─ db.py, models.py      # SQLAlchemy + models
│  ├─ clients/hardcover.py  # GraphQL client
│  ├─ clients/indexer.py    # Indexer protocol + get_indexer()
│  ├─ clients/audiobookbay.py       # HTML-scraping indexer
│  ├─ clients/download_client.py    # DownloadClient protocol + get_download_client()
│  ├─ clients/deluge.py     # Deluge web JSON-RPC
│  ├─ clients/qbittorrent.py        # qBittorrent Web API v2
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
5. ✅ **Release search & grab** — release picker, grab flow, release tracking.
   *(Originally Prowlarr; replaced by direct AudioBookBay search + Deluge — see below.)*
6. ✅ **Watch & import** — download watcher, importer with Audiobookshelf-style renaming, activity page, failure handling.
7. ✅ **Polish & ship** — settings page with connection checks, library filters/sort, docker-compose example, README, test pass.

Each milestone ended in a working, verified state (one commit per milestone).

### Direct torrent pipeline (replaced Prowlarr) — complete

Prowlarr is gone: the app now owns the whole pipeline, which is what lets it report real
download progress instead of guessing from file mtimes.

1. ✅ `Indexer` protocol + AudioBookBay scraping client.
2. ✅ `DownloadClient` protocol + Deluge web JSON-RPC client.
3. ✅ Cutover — config, `release` schema migration, service/routes/templates, Prowlarr deleted.
4. ✅ Watcher polls the client by info hash for progress/completion, with the directory
   watcher as fallback.
5. ✅ Docs.

Remaining manual check (needs the user's own Deluge and a real book): grab a release from
the UI and watch it download → import end to end. Search, magnet building, client status
polling and import-from-a-finished-torrent have each been verified live; only a real grab
(which permanently adds a torrent to the user's Deluge — the app deliberately cannot remove
it) is left to the user.

## Testing

- Unit tests for state mapping, filename sanitization, path templating, release↔download matching.
- Client tests against recorded/mocked HTTP (respx): Hardcover, AudioBookBay (canned HTML
  fixtures in `tests/fixtures/abb/`), Deluge (JSON-RPC answered by request method).
- An importer integration test using tmp dirs with fake download layouts (single m4b, multi-file mp3, nested folders).
- Watcher tests for both paths: the download client reporting progress/completion (stubbed
  client), and the fallback when it is unreachable or doesn't know the hash.

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
- **Identification**: each entry is identified by **searching Hardcover** (query built
  from the entry name with parent folders as author/series hints), scoring the results
  with normalized name similarity vs title / author+title / series+index+title forms.
  High-confidence matches are pre-selected; low-confidence ones are suggestions; below
  threshold the entry is unmatched. Identification runs with the requesting user's token
  (public book search — any account works) and is cached per entry in the app_state table
  (`imports_match:{rel}`), with a per-row "Re-identify" button to bust the cache.
- **Amend**: each row's match can be changed — pick from local-library search (unimported
  tracked books) or from a Hardcover search. Choosing a match only stores it; **nothing is
  shelved on Hardcover from the Imports page**.
- **Import**: per-row button, bulk-select, or import-all-matched. Import always **moves**
  (regardless of IMPORT_MODE, which remains download-pipeline-only): the Book row is
  created from Hardcover metadata if nobody tracks it yet (**ownerless** — no user_book),
  files go to the standard edition path (`Author/Series/{index} - Title/`, label-suffixed
  for labelled editions), the source folder is removed from /imports, and now-empty
  parent folders are cleaned up. Files move **one at a time** through the same
  `collect_files` the download pipeline uses (`keep_unknown=True`, since these folders are
  the user's own curation rather than a torrent's junk — anything filed alongside the audio
  comes along), so audio gets the same content identification and mislabelled extensions
  are corrected. Disc subfolders are preserved and then pruned once emptied, so the entry
  still drains. A rejected file (a video sample) is **left in /imports, never deleted** —
  this import moves, so dropping it would destroy the user's only copy, and the leftover
  keeps its folder out of the cleanup as the visible signal that the entry did not fully
  drain. An entry with no usable audio fails *before* anything destructive runs (notably
  before a replace deletes the old edition's files). The target edition becomes
  `download_state=imported` with `library_path` set. Users who have the book in their Hardcover library pick it up
  automatically — a successful batch kicks a background all-user sync; everyone else sees
  it as *available* in search. Destination-exists and other failures are reported per
  row; nothing is guessed.
- **Choosing the edition**: each matched row folds its whole edition choice into one
  "Edition…" disclosure, which lazily loads a picker of radio options in the same order
  as the add-edition dialog — *my own label* (a free-text box that is itself the option),
  labels the series uses elsewhere, this book's own editions (**replace**), then the rest
  of Hardcover's audiobook editions. The selected option decides the label
  (`edition_choice(pick_wins=True)`: unlike the download pickers' override box, text left
  behind after a change of mind is ignored) and stamps its Hardcover edition id and
  narrator onto the `edition` row. Choosing nothing, or an empty own label, imports to
  the plain unsuffixed folder. Collapsed, the summary reads "Edition: <choice>".
  Fields are namespaced by the entry's rel path because the Import selected/all buttons
  post the whole table in one request. The picker is lazy because it costs a Hardcover
  fetch plus a header read of every audio file: opening it badges the edition whose
  `audio_seconds` is within 5% of the files' total runtime, and any edition whose
  narrator is named in the folder path. **Hints are advisory badges only — nothing is
  ever preselected**, so a wrong guess can't silently mislabel an import.
- **Replacing**: picking one of the book's own editions deletes that edition's library
  files and puts these there instead, under the same label (`import_entry`'s
  `replace_edition_id`; deletion is committed before the move, so a failed move leaves
  the edition file-less rather than lying about its path). The browser confirms at
  *selection* time, naming the edition and folder — a bulk import must not fire a
  confirm per row, and nothing is deleted until Import runs.
- **Additional editions**: an entry matched to an already-available book imports as
  another edition — it needs its own label, or an edition to replace. Guardrails mirror
  the download flow: an unlabelled import into an available book refuses ("give these
  files an edition label"), as does a labelled import while the existing files are
  unlabelled (rename them on the book detail page first).
- **Safety**: import paths are validated to stay inside IMPORTS_DIR; an edition whose
  label already has files can't be the target of an import.

## Audiobookshelf-compatible API

Implement enough of the Audiobookshelf server API (https://api.audiobookshelf.org) that ABS
client apps can log in, browse, stream, download, and sync progress against this server.
Primary compatibility target: the **official ABS app** (Android/iOS); third-party clients
(Plappa, ShelfPlayer) should mostly work as a byproduct and get quirk fixes on demand.

Decisions:
- **Auth**: user-account credentials (the virtual admin is rejected — it has no library).
  `POST /login` (JSON) issues a signed bearer token (same secret as the session cookie)
  whose `userId` is the account's stable uuid; ABS endpoints authenticate via
  `Authorization: Bearer` and re-check the account is enabled on every request. The UI's
  form login and cookie session are unchanged — `/login` dispatches on content type.
  `/status`, `/ping` stay public (server discovery).
- **Streaming**: direct play only — original m4b/mp3 files served with HTTP Range support.
  No ffmpeg/HLS. (ABS apps direct-play these formats natively.)
- **Progress**: stored locally per user per *edition* (cross-device resume). When a
  client reports any edition finished, mark the book read on Hardcover with today's date
  (same code path as the UI toggle).
- **Catalogue**: one library item per imported *edition* (`li_<edition.id>`,
  `library_path` set); one fixed library ("Audiobooks"). A book with 2+ editions shows
  each item with its label in the title ("… (Stephen Fry)") and the edition's narrators
  in metadata/filterdata. Covers served from a cover image in the edition folder when
  present, else proxied from the Hardcover CDN URL.

New persistence:
- `audio_file` — per-edition audio tracks: index, relative path, size, mime, duration
  (+ chapters for m4b), read with **mutagen** at import time, with a startup backfill scan
  for already-imported editions.
- `media_progress` — per-user progress per edition: current_time, duration, is_finished,
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

## Multi-user conversion (complete)

Mandatory accounts replace the optional single-user login. Design summary (fresh database
assumed; the Alembic history was squashed to a single initial revision — no data
migration from the single-user schema exists):

- **Accounts**: `user` table (uuid for the ABS userId, unique username, scrypt password
  hash with parameters embedded in the stored string, per-user `hardcover_token`, `enabled`
  flag, per-user sync cursor/result). The `admin` account is virtual — checked against
  `ADMIN_PASSWORD`, never stored — and sees only the user-administration UI. Disabling a
  user takes effect immediately (sessions and ABS tokens are re-checked against the DB per
  request).
- **Per-user library**: a `user_book` join row carries `read_state`, `read_at`,
  `pending_push`, and `hardcover_user_book_id`; those columns leave `book`. Each user's
  Hardcover sync runs with their own token and maintains their own `user_book` rows over
  shared `book` metadata rows. `media_progress` and `bookmark` gain `user_id` (ABS progress
  is per user); `release` records which user grabbed it (Activity stays global).
- **Shared store**: `/audiobooks` and the pipeline state are shared — `download_state`
  and `library_path` live on `edition` rows under `book` (see "Multi-edition support");
  a book's display status aggregates its editions (not present / downloading /
  available; a failed download shows as not present plus a failure badge and the
  Activity entry). A book another user made available cannot be grabbed again — search
  offers "add to my library" instead (which shelves it on the searcher's Hardcover) and
  the book detail page offers "Download another edition". Book rows may belong to no
  user ("available" only).
- **Imports**: staged folders are identified by searching Hardcover (author/series/title
  heuristics from folder names), moved to the canonical path, and imported as ownerless
  books — users who have the book in their Hardcover library pick it up automatically on
  their next sync. No Hardcover shelving side-effects.
- **Deleting a user**: books only they had are reviewed — per-book choice of delete from
  disk (removes files, book row, everyone's progress) or leave in place (stays available);
  metadata-only orphans are removed automatically.

Milestones (commit each; the app stays runnable throughout):
1. ✅ **Auth foundation** — user table + scrypt hashing, `ADMIN_PASSWORD` + startup guard,
   mandatory-login middleware (admin ↔ user route separation), login/logout rework,
   minimal admin Users page (list + add), ABS auth switched to DB accounts (tokens carry
   the user's uuid; admin rejected; disabled users' tokens invalid).
2. ✅ **Admin user management** — enable/disable, change password, change token, simple delete.
3. ✅ **Per-user data pivot** — `user_book`, `user_id` columns, per-user sync loop, UI rework
   (library/search/downloads), drop the global `HARDCOVER_TOKEN`.
4. ✅ **ABS per-user** — progress/bookmarks/sessions filtered by the authenticated user.
5. ✅ **Imports rework + delete-user orphan review.**
6. ✅ **Squash migrations + final docs pass.**

## Limited accounts (complete)

`user.role` is `full` (everything above, the default) or `limited`: a listener who reaches
the library only through Audiobookshelf apps.

- **What a limited account can do**: exactly the ABS surface — log in over JSON, browse and
  play every available edition, keep its own progress and bookmarks, sign devices in and
  out. Nothing in `app/abs/` distinguishes it; the catalogue is library-wide and driven by
  `media_progress`, which never touched `user_book` anyway.
- **What it cannot do**: the web UI. `RequireAuthMiddleware` turns it away (`/login?error=app_only`)
  and the login form refuses it with 403 — the credentials are valid, this door is not
  theirs. Because the middleware re-reads the row per request, demoting a signed-in user
  locks them out on their next request, which matters as browser sessions are signed
  cookies with nothing to revoke. ABS sessions are deliberately left alive: app access is
  precisely what a limited account keeps.
- **No Hardcover, enforced**: the token is forced empty on create and cleared on demotion,
  `POST /admin/users/{id}/token` refuses (422), and the sync selects filter on the role.
  Listening skips read state entirely — `_set_read_state` in `app/abs/progress.py` returns
  early, the single choke point every progress route funnels through — so no `user_book`
  row is created and nothing is left `pending_push`. The rest of `apply_progress` still
  runs: `is_finished`, resume, and the near-finish sweep behave exactly as for a full user.
- **Promotion/demotion** keeps existing `user_book` rows, so promoting a demoted account
  restores its library; the admin then sets a token.

## Multi-edition support (complete)

A book can hold several audiobook recordings — e.g. Chamber of Secrets in the Stephen
Fry, Jim Dale, and Full-Cast recordings — downloaded, stored, and served side by side.
Built as an experiment (the user may not keep it), but first-class.

- **Edition identity**: an `edition` row per recording (unique `book_id`+`label`),
  optionally tied to a Hardcover *edition* id when picked from the live editions list
  (query pinned under "Hardcover" above). The **label** is the grouping name used in
  folder names ("Stephen Fry", "Full Cast"); it defaults from the Hardcover edition's
  narrators (ensembles of 4+ default to "Full Cast") and is user-editable. Labels
  already used across the book's series are suggested so groups line up. `""` is the
  unlabelled edition — at most one per book.
- **Folder layout — no unsuffixed edition once labelled**: a labelled edition of a
  series book suffixes the **series** folder, grouping the whole series' recordings:
  `J.K. Rowling/Harry Potter {Stephen Fry}/2 - Harry Potter and the Chamber of Secrets/`.
  Standalone books suffix the book folder: `Andy Weir/Project Hail Mary {Ray Porter}/`.
  Only the unlabelled edition uses the plain unsuffixed path. A single-edition book
  whose label is known lives at its labelled path too ("by its own edition label").
- **Renames happen at label-assignment time** (`relabel_edition`): the folder moves to
  the label's location immediately — filesystem first, DB committed only after the move;
  occupied destinations are refused; emptied parents cleaned up. AudioFile rel_paths are
  relative to the edition folder, and ABS file inos are the audio_file row ids, so both
  survive renames. Relabelling moves only that book's folder — series siblings move when
  their own editions are labelled (no bulk moves; a "relabel all series siblings" helper
  is possible future work). The rename control lives on the book detail page: a
  "Rename…" button per edition opens an overlay carrying the *same* picker the download
  flows use (series-sibling labels first, then the book's remaining Hardcover audiobook
  editions, then a free-form box that overrides the selection), with a Rename button to
  confirm — no bare text field. Picking an entry re-stamps the edition's
  `hardcover_edition_id` and narrator (the old narrator belonged to the old label); a
  typed label leaves both alone. Submitting nothing drops the label, moving the files
  back to the unsuffixed folder. A refused move (duplicate label, occupied destination)
  keeps the dialog open with the reason; success closes it and swaps the detail page's
  editions section out of band.
- **Adding an edition**: the detail page's "Download another edition…" opens a
  three-section picker. First, **"Add existing edition"**: every edition label sibling
  books in the series already have but this book lacks
  (`series_edition_candidates`), matched to the book's Hardcover audiobook editions by
  default label — a matched entry shows that edition's Hardcover info and carries its
  edition id, an unmatched one offers the label alone with the sibling's narrator (so
  series folders line up even when Hardcover data is missing or unreachable). Second,
  the book's *existing* editions as clearly-marked "replace this edition's files"
  entries (shortcuts into the per-edition replace flow). Third, the remaining Hardcover
  audiobook editions (`reading_format_id = 2`, `users_count desc` — books can carry
  dozens of junk/foreign editions; sibling-matched ones are not repeated here) plus a
  free-label input. In the dialog every entry in all three sections is a one-click
  button (a pick submits straight into the release search); the first-grab picker uses
  radios instead, since the choice rides along with each release row's Grab form. Each
  Hardcover option leads with the label it would apply ("Full
  Cast", or "Unnamed edition" when no narrators are listed) with
  narrator/format/duration/publisher as a secondary line; editions already downloaded
  (matched by Hardcover edition id or label) are hidden. If the
  existing edition is unlabelled it must be labelled in the same dialog (its folder moves
  right then) — enforcing "all editions carry a suffix once there are two". The release
  picker then carries the edition choice into the grab; the per-edition guard replaces
  the book-level available/downloading block for additional editions. A *first* grab may
  optionally pick an edition too (defaults to the unlabelled edition); its lazy edition
  picker shows the same "Add existing edition" section above the Hardcover list (no
  replace section). That picker is collapsed behind an "Edition (optional)" toggle only
  when the book's series has no labelled editions to offer; when it does
  (`series_edition_candidates` is non-empty) the release dialog loads the picker
  immediately with those sibling labels on show and folds just the Hardcover list and
  free-label box away behind "Or download a new edition", so a book joining a labelled
  series lands in the right group by default. On the Imports page, the edition-label input of a row matched to an
  available book suggests the series' labels via a datalist (minus labels this book has
  already imported); no Hardcover editions fetch there. Editions rows are
  created at grab/import time, never by the dialog alone.
- **Replace is per edition** (`?replace=1&edition_id=N`): the new download lands on the
  same edition row; siblings are never touched.
- **ABS**: one library item per imported edition (`li_<edition.id>`); per-edition
  progress and bookmarks; multi-edition items carry their label in the display title;
  narrators surface in metadata and filterdata. **Read state stays book-level**:
  listening to any edition moves the book on the user's Hardcover shelf.

### Listening drives read state

Progress syncs from ABS clients (all of them funnel through `apply_progress` in
`app/abs/progress.py`) maintain the user's Hardcover shelf:

- **Started** — past `MARK_READING_AFTER_MINUTES` (default 1) the book becomes *currently
  reading*, `started_at` = today, pushed as `first_started_reading_date`. Once per book; a
  book already marked *read* promotes too (playing it again is a re-listen). 0 promotes on
  the first progress sync after playback starts.
- **Finished** — the ABS rule (remaining <= 10s, or the client says so) marks it *read*,
  `read_at` = today.
- **Near-finished** — the trailing credits usually go unplayed, so a book left within
  `MARK_READ_TAIL_MINUTES` of the end (default 30) is marked finished and *read* as soon as
  progress arrives for a **different book**. Other editions of the same book don't count as
  another book. The tail is capped at half the duration (a 30-minute tail must not make a
  20-minute book readable from its first second); 0 requires a complete listen.
- **Re-listen** — a finished progress row resynced at least 60s in but before the
  near-finish mark is un-finished, which is what lets the started rule fire again. Nothing
  else clears `is_finished` on its own. The 60s floor is fixed rather than following
  `MARK_READING_AFTER_MINUTES`, which may be 0.

Both thresholds are plain durations — there is deliberately no percentage-of-book rule.
- **Migration**: the editions rollout added one unlabelled edition per book with
  pipeline state, at a path byte-identical to the old book path — DB-only, no files
  moved. ABS item ids changed (`li_<book.id>` → `li_<edition.id>`); server-side progress
  migrated, apps re-fetch and may 404 one stale sync. That revision has since been
  squashed away: the Alembic history is now the single revision `4279694b0300`, which
  creates the current schema outright.

## Future work (out of scope for this build)

- Public REST API: list books, download audio files, update reading state — designed to serve
  the companion app below (the internal routes should keep this in mind but v1 does not expose it).
- Android/iOS audiobook player companion app (similar to Smart Audiobook Player) that
  downloads from the library, plays audiobooks, and updates read status.
