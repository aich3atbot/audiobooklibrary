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
| Users | **Multi-user, mandatory login** (signed session cookie naming a server-side session row, so a login can be revoked). An `admin` account (password from `ADMIN_PASSWORD`, which creates it on a fresh database) only administers users: add, enable/disable, delete, change passwords/tokens. Regular users are DB rows (scrypt password hashes) created by the admin, each with their own Hardcover token. An account is **full** (web UI + Hardcover) or **limited** (Audiobookshelf apps only — see "Limited accounts" below). "admin" is a reserved username. See "Multi-user conversion" and "Revocable sessions" below for the full design. |
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
  role (`full` | `limited` — see "Limited accounts" — or `admin`), enabled, created_at,
  last_sync_at, last_sync_result. The `admin` account is one of these rows; see
  "Revocable sessions".
- **auth_session** — id, uuid (the client-facing session id), user_id, kind (`abs` | `ui`),
  token_hash, last_token_hash + last_token_expires_at (ABS rotation grace), expires_at,
  user_agent, ip_address, created_at, updated_at. One row per live login of either
  kind; see "Revocable sessions".
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
- **Settings page** (`/settings`) — connection status for Hardcover, the indexer, the
  download client and ffmpeg, sync interval, paths (read-only display of env config),
  "Sync now" button.

### Live Activity page (built)

The book detail page already updates itself while a conversion runs; Activity does not, so
the one page whose whole job is showing work in flight is the one you have to reload. It
should refresh itself for **both** downloads and conversions.

- **Fragment + self-chosen interval.** Everything below the `<h2>` moves into
  `_activity_content.html` under `id="activity"`; `GET /activity` returns the whole page
  normally and just that fragment when the `HX-Request` header is present (the pattern
  `cancel_transcode` already uses). The fragment's root carries
  `hx-get="/activity" hx-trigger="every {{ poll_seconds }}s[…]" hx-swap="outerHTML"`, and
  the *server* picks the interval: **5s when anything is active** (a release in
  `ACTIVE_STATUSES` or a job in `TRANSCODE_ACTIVE`), **30s when nothing is**. Because each
  swap re-renders the attribute, a page that goes quiet slows itself down and one that gets
  busy speeds up, with no JavaScript of ours and no risk of a tab polling hard all day.
  Refreshing everything — including *Recently imported* — means a finished download is seen
  moving from one section to another.
- **An open form is never clobbered.** A periodic swap would collapse an open "Manual
  import…" disclosure and discard whatever folder name was being typed into it, so the
  trigger carries a filter:
  `every 5s[!document.querySelector('#activity details[open], #activity :focus')]`.
  Checked against htmx 2.0.10's own tokenizer and `maybeGenerateConditional`: `every`
  accepts an event filter, the `[open]` inside the selector survives because bracket
  counting is balanced, and `processPolling` re-schedules the timer *even when a tick is
  filtered out* — so the refresh pauses while you type and resumes when you close the box,
  rather than stopping for good.
- **The watcher gets the same treatment.** Page polling alone would show download progress
  advancing in 30-second steps, because that is how often the watcher asks the torrent
  client. `download_watch_loop` sleeps `ACTIVE_WATCH_SECONDS` (10) when its last pass saw
  active releases and `watch_interval_seconds` when it did not — never *longer* than the
  configured interval, so `WATCH_INTERVAL_SECONDS` stays both the idle cadence and the
  ceiling for an operator who wants it slower. `scan_downloads_once` already loads the
  active releases, so it returns that count rather than the loop asking again. No new env
  var: this is a floor on responsiveness, not a knob.
- **Matching granularity.** `PROGRESS_EVERY` (transcode progress writes, every 25 ffmpeg
  progress lines ≈ 6s) drops to 10 (≈2.5s), so a 5s poll actually shows movement rather
  than the same number twice. The cost is one extra tiny commit every few seconds.
- **Not** websockets or SSE: there is no such machinery in this app, polling is what the
  rest of the UI does, and an idle page costs two requests a minute.

Watching a real 61-minute conversion through this is what turned up the stderr deadlock
described under "Transcoding" — the page did its job and showed the encode frozen at 48%.

## Configuration (env vars)

The defaults below are applied by `app/config.py`, which is the only place they are
written down: `docker-compose.yml` lists the optional variables bare so an unset one is
never passed into the container, and a blank value is treated as absent (except where the
default is itself empty — there empty is a real setting). Everything but `ADMIN_PASSWORD`
and the four bind-mount paths can simply be left out.

```
ADMIN_PASSWORD          # password for the "admin" account. Required on a fresh database
                        # (it creates the account); afterwards optional — while set it is
                        # authoritative at every startup, unset the stored one stands
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
                        # The four paths below name a *host* directory in .env, which
                        # docker-compose bind-mounts onto the container path they default
                        # to; they are never passed into the container, so there they are
                        # always the default. Running outside a container (dev, tests)
                        # they are the paths the app uses directly.
DOWNLOAD_DIR            # -> /downloads — the client's *completed*-downloads directory
LIBRARY_DIR             # -> /audiobooks
CONFIG_DIR              # -> /config (sqlite db + session_secret)
IMPORTS_DIR             # -> /imports (staging area for an existing collection)
SYNC_INTERVAL_MINUTES   # default 30
WATCH_INTERVAL_SECONDS  # default 30 (download dir poll)
DOWNLOAD_QUIET_SECONDS  # default 120 (download "finished" quiet period)
IMPORT_MODE             # copy (default, hardlink-or-copy) | move
TRANSCODE_BITRATE       # default 64k — target AAC bitrate for MP3 -> M4B; halved for a
                        # mono book, never above the source's own bitrate
FFMPEG_PATH             # default "ffmpeg" (the image builds its own 2 MB copy)
PUID / PGID             # compose-only, optional (default 1000:1000): the uid:gid the
                        # container runs as (docker-compose `user:`), owning written files
```

## Container

- Single `Dockerfile` (python:3.12-slim, uv or pip install, uvicorn entrypoint), with a
  first stage that builds a **2 MB purpose-built ffmpeg** for MP3 → M4B conversion (see
  the transcoding section for why not Debian's 434 MB package).
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
│  ├─ services/transcode.py # MP3 -> M4B: chapter sources, ffmpeg, the job worker
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
- **Auth**: user-account credentials (the admin account is rejected — it has no library).
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
  flag, per-user sync cursor/result). The `admin` account is a row of the same table with
  `role = admin` (see "Revocable sessions") and sees only the user-administration UI.
  Disabling a user takes effect immediately (sessions and ABS tokens are re-checked against
  the DB per request).
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

## Revocable sessions (complete)

Every login of either kind is an `auth_session` row, so signing out actually signs out.
ABS refresh tokens got rows first; browser logins followed, which needed the admin to
become a real user (`auth_session.user_id` is a foreign key to `user`, and the admin had
no row to point at).

- **Browser sessions** (`kind = "ui"`): the signed cookie carries one opaque random token
  (`sid`); the row it hashes to is the session. `RequireAuthMiddleware` resolves it per
  request, so deleting the row logs the browser out immediately — from a password reset,
  a sign-out, or an app's device list. Sessions live 30 days and **slide** as they are
  used, matching the cookie's own `max_age` (Starlette re-sends it on every response), but
  the row is only touched once an hour so an ordinary page view stays a read.
  Browser sessions never rotate; `last_token_hash` stays null for them.
- **The device list**: `/api/me/sessions` lists both kinds, as upstream does, and
  `DELETE /api/me/sessions/:id` revokes either — a phone can sign a laptop out. A browser
  row is never `current` there (currency is resolved from the request's refresh token).
- **Password changes revoke everything.** An admin reset drops every session the user has,
  browser and app alike; whoever knew the old password is out on their next request.
- **The admin account** is a `user` row with `role = admin`, username "admin" (still
  reserved). A role rather than a flag on purpose: the Hardcover selects already filter
  on `role == full`, so the admin is excluded from sync, token-borrowing and the metadata
  backfills for free. It is invisible to /admin/users and `_get_user` refuses it, so no
  verb can disable, delete, demote or re-password it; the ABS surface rejects it in four
  places (JSON login, `require_abs_user`, `/auth/refresh`, socket.io auth) because it now
  has a uuid a hand-made token could name.
- **`ADMIN_PASSWORD` reconciles the account at startup** (`ensure_admin_account`): it
  creates the row on a fresh database and refuses to start without one; while it stays set
  it is authoritative, and changing it rehashes and revokes the admin's sessions; unset,
  the stored password stands. It is the only way to set that password — there is no
  self-service password change in the UI.
- **Still not covered**: a *self-service* session manager. Users cannot see or revoke their
  own browser sessions from the web UI (only from an ABS app, or by logging out); the
  admin's lever is a password reset or disabling the account. Rotating
  `CONFIG_DIR/session_secret` remains the blunt instrument — it invalidates every cookie
  and every ABS token at once.

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
  locks them out on their next request. ABS sessions are deliberately left alive: app
  access is precisely what a limited account keeps.
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

## Transcoding MP3 editions to M4B (built)

An edition whose files are a pile of MP3s can be converted, on demand, into a single
chaptered `.m4b`. This is a *library* operation, not a download one: it rewrites files
already imported, and it is destructive (the MP3s are deleted afterwards), so every part
of it is designed around "prove the new file is good before removing the old ones".

### Where it is offered

On the book detail page, inside an edition's **expanded file list** (`_edition_files.html`,
lazily loaded from `/editions/{id}/files`) — that fragment already walks the folder and
probes every file with `identify()` for the bitrate column, so the predicate costs nothing
extra and lands exactly where the user can see what would be converted.

`transcodable(edition)` is true when **all** of:

- the edition has a `library_path` and the folder exists;
- it holds at least one file whose *contents* are MP3 (`identify().family == "mp3"` — by
  contents, never the extension, like everything else in the app) and **no other audio**:
  no file in `AUDIO_EXTS` and no file identifying as another audio family. Subfolders
  (CD1/CD2) count and are walked;
- ffmpeg is available (probed once, cached like `downloads_enabled`);
- the edition has no queued/running transcode job and no active release (a replace
  download in flight would land on the same folder).

The button opens a confirm dialog (destructive, irreversible) naming the file count, the
target bitrate, where the chapters will come from, and what will be deleted. It also warns
when the MP3s are the *only* copy: with the default `IMPORT_MODE=copy` the library files
are hardlinks to the still-seeding torrent, so deleting them costs nothing, but a `move`
import or a collection import leaves no second copy.

### Encoding

Shell out to `ffmpeg` (`FFMPEG_PATH`, default `ffmpeg`). **ffprobe is deliberately not
used**: the only thing it was wanted for is per-file durations, which mutagen already
reports accurately for MP3 (Xing header / frame count) and which `audio_file.duration`
already holds from the import scan. Not needing it halves the cost of shipping ffmpeg.
There is no lossless path — MP3 must be re-encoded to AAC (remuxing MP3 into an MP4
container is legal but half the players choke on it, so it is not an option here).

The exact command below has been **run for real** (see "Verified live" at the end of this
section) against mixed-format MP3s:

```
ffmpeg -nostdin -hide_banner -y \
  -i 01.mp3 -i 02.mp3 … -i chapters.ffmeta \
  -filter_complex "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a0]; …
                   [a0][a1]…concat=n=N:v=0:a=1[out]" \
  -map "[out]" -map_metadata N -map_chapters N \
  -metadata title=… -metadata album=… -metadata artist=… -metadata composer=<narrator> \
  -f ipod -c:a aac -b:a <rate> -movflags +faststart -progress pipe:1 -nostats \
  "<folder>/.<name>.m4b.part"
```

- **`-f ipod` is mandatory**, not cosmetic: the output is written to a `.part` temp name,
  so ffmpeg cannot infer the muxer from the extension and fails without it.
- `-map_metadata`/`-map_chapters` take the *input index* of the FFMETADATA file, which is
  last, so both are `N` (the number of MP3s).

- The **concat filter with a per-input `aformat`**, not the concat demuxer: a book's MP3s
  are routinely mixed (22.05/44.1 kHz, mono/stereo) and the demuxer does not resample
  between segments. Inputs beyond a few hundred fall back to the demuxer with a temp list
  file (command-length guard).
- **ffmpeg's stderr goes to a file, never a pipe.** The worker consumes stdout line by
  line for progress and only reads stderr at the end, so a stderr *pipe* fills at 64 KB
  and blocks ffmpeg mid-encode — the job then sits at RUNNING forever holding a
  half-written `.part`, until a restart fails it. Not theoretical: a 61-minute test book
  whose MP3s had damaged frame headers made ffmpeg emit **132 KB** of "Header missing" and
  froze the encode at 48%, which is exactly the shape of a torrent-sourced rip. Damaged
  frames are common enough that the noisy case is the one to design for, and
  `test_a_chatty_ffmpeg_does_not_deadlock` holds the line (in a daemon thread, so a
  regression fails in 60s instead of wedging the suite).
- **Bitrate**: `TRANSCODE_BITRATE` (default `64k`), halved when every source is mono, and
  capped at the highest source bitrate so a 32k MP3 is never "upscaled".
- Cover art: an image already in the folder (`cover.*`/`folder.*`, else the only image) is
  embedded as the MP4 cover; the file itself stays where it is.
- Progress comes from `-progress pipe:1` (`out_time_us` against the summed source
  duration), written to the job row every few seconds.

### Chapters

Chapters can be *embedded in the MP3s* as well as sitting beside them, and embedded data
wins: it travels with the files and, for an edition we have already imported, it is what
the ABS apps are showing right now. Sources, first hit wins:

1. **ID3v2 `CHAP` frames inside the MP3s** — the ID3 chapter-frame addendum, one `TIT2`
   sub-frame per chapter title, times in ms relative to the file. **The app already reads
   these**: `audio_meta._read_chapters` does `tags.getall("CHAP")`, and for an imported
   edition the result is already sitting in `audio_file.chapters_json`. So the transcode
   does not re-parse anything — it takes each file's stored chapters and shifts them by
   that file's start offset, which is the *identical* rule the ABS API already uses to
   build a book's chapter list (see CLAUDE.md, "a book's chapters are its files' chapters
   shifted by each file's start offset"). The m4b therefore inherits exactly the chapter
   list the user already sees in their app — the conversion changes nothing they can
   perceive except the file count.
2. **OverDrive MediaMarkers** (**built** — see "Embedded chapter sources" below) — a
   `TXXX` frame with description `OverDrive MediaMarkers` whose value is XML
   (`<Markers><Marker><Name>…</Name><Time>0:00.000</Time></Marker>…`), times relative to
   the file. Extremely common in library-sourced MP3 audiobooks. It lives in
   `_read_chapters`, not in the transcoder, so it improves the ABS chapter list for every
   existing MP3 edition and the transcode inherits it for free.
   **Triviality demotion**: embedded chapters that amount to exactly one chapter spanning
   a whole file are not chapter information — they restate the file boundary, which the
   fallback already knows. Treat those as *absent* so a real sidecar below can win;
   otherwise a folder of one-trivial-CHAP-per-file MP3s would shadow a hand-made 40-entry
   `.cue`. This is the only case where a sidecar outranks embedded data.
3. **`*.cue`** in the folder. Both real-world shapes: one `FILE` (times absolute over the
   whole book) and one `FILE` per MP3 (times relative to that file, offset by the file's
   start in the concatenation, matched by filename). `INDEX 01 mm:ss:ff` frames are 1/75s.
   **A `FILE` that names one of our tracks is always placed against it**, however many
   `FILE`s the sheet has; only a *lone* unrecognised one means "absolute". That is what
   makes a **per-disc set** work: a multi-disc rip ships one sheet per disc, each timed
   from its own disc's zero, so the shallowest-wins rule alone would chapter disc one and
   leave the rest bare with its last chapter stretched over the remainder. A sheet
   anchored to real tracks is therefore merged with every other anchored sheet in the
   folder (`_cue_pass`), and all of them count as consumed. A whole-book sheet anchors
   nothing and still stands alone. Matching is by bare filename, so **a stem two tracks
   share (`CD1/Track01.mp3`, `CD2/Track01.mp3`) resolves to neither** — `track_offsets`
   drops it, and the sheet naming it is refused rather than placed on a coin-flip disc.
4. **`chapters.txt` / `*.chapters.txt`** — `HH:MM:SS(.mmm) Title` or `MM:SS Title` lines.
5. **An FFMETADATA file** (`*.ffmeta`/`*.ffmetadata`, or any sidecar starting with
   `;FFMETADATA1`) — parsed through our own reader rather than trusted wholesale.
6. **An Audiobookshelf `metadata.json` / `metadata.abs`**, if the folder came from an ABS
   library — it carries a `chapters` array in our own shape. Cheap to support, and this
   app is ABS-compatible everywhere else.
7. **Fallback: one chapter per MP3**, titled from the filename stem verbatim
   (`03 - The Vanishing Glass.mp3` → `03 - The Vanishing Glass`). Disc subfolders are
   ignored in the title. This is `catalogue.edition_chapters`, which the transcoder calls
   rather than reimplementing — the m4b's chapters are then *by construction* the ones the
   ABS apps were already showing.

**The title tag never beats a filename**, and this was settled by measurement against the
real library rather than by preference. Of its three MP3 editions:

| edition | files | filenames | title tags |
|---|---|---|---|
| Chamber of Secrets {Full Cast} | 20 | `Chapter 1 - The Worst Birthday` … | **all 20 identical**: the book title |
| Chamber of Secrets {Stephen Fry} | 20 | `Chapter 1 - The Worst Birthday` … | all 20 identical — and naming the *wrong* edition ("Full-Cast Edition") |
| Four: A Divergent Collection | 52 | `Four - D01T02 Part 1 - The Transfer` … | **none at all** |

Nought for three. Preferring tags would have turned two perfectly-titled books into forty
chapters named after the book, one of them mislabelled. So: the filename wins, and the tag
is read only when the name says nothing but a position — `003.mp3`, `track_07.mp3`,
`CD2/04.mp3`, `Chapter 12.mp3` (`catalogue.JUNK_STEM` / `track_chapter_title`).

That same measurement exposed a failure mode worth guarding directly, since the tag
pattern it depends on is demonstrably common: **tags are ignored wholesale unless they
vary across the edition and are more than the book's own name**
(`catalogue.tags_can_name_chapters`). Without it, a book of `001.mp3 … 040.mp3` files all
tagged with the book title — exactly what these editions carry — would render forty
identically-named chapters. Both rules are **built**.

Two sources are rejected outright: **silence detection**
(`silencedetect` guesses boundaries, and this project never guesses a match) and
**playlist files** (`.m3u`), which give order, not chapters — order already comes from the
natural-sort that produced `audio_file.index`, and must keep coming from there so
positions stay put.

**Embedded chapter sources (built, ahead of the transcoder)** — sources 1, 2 and 7 landed
on their own, because they improve the ABS chapter list for every MP3 edition already in
the library, with or without transcoding:

- `audio_meta._overdrive_chapters` parses the marker XML; `_read_chapters` tries CHAP
  first, then markers, then the MP4 container. Marker times are `H:MM:SS.mmm` /
  `MM:SS.mmm` / `SS.mmm`; ends close on the next marker and the last stays open for
  `scan_edition_audio` to close with the track duration. A chapter split across files
  repeats its marker at the same instant, so a marker at or before the previous one is
  dropped rather than becoming a zero-length chapter.
- `audio_file.title` (revision `a4e1c7b90d33`) stores each track's own title tag —
  ID3 `TIT2`, MP4 `©nam`, Vorbis `title` — read during the scan.
- `CHAPTER_SCAN_VERSION` is **3**, and its re-scan is now *every* imported edition rather
  than only the MP4s version 2 looked at: markers apply to MP3s and the title column is
  new on every row, so no narrower predicate would find the rows that need filling.
  Header-only, so a full pass stays cheap.

Everything normalizes to `[(start, title)]`, sorted, deduped, clamped, ends closed with
the next start (last = total duration), and is written as an FFMETADATA file with
`TIMEBASE=1/1000`. A source that parses to nothing usable falls through to the next.
ffmpeg writes MP4 chapters as a QuickTime chapter text track, which
`app/services/mp4_chapters.py` already reads; the post-transcode rescan is the proof.

**Offsets come from a decode pass, not from the tags** (`measure_durations`). Tag
durations are systematically long — measured at **+0.8% on every file** of a mixed test
set, because the frame count includes the encoder delay and padding a gapless decoder
trims. While each file is its own track that is invisible; concatenated into one it
accumulates, and 0.8% of a ten-hour book is nearly five minutes of chapter drift by the
end. So each input is decoded once (`-f null -`, which is why the build needs the `null`
muxer and a `pcm_s16le` encoder) and its real length used for every offset. On the test
set this moved the chapter marks from 0 / 5.042 / 12.095 to exactly 0 / 5.0 / 12.0 and cut
the whole-file duration error from 105 ms to 21 ms. The pass costs one cheap decode: MP3
decodes an order of magnitude faster than the AAC encode that follows, and a file that
cannot be measured falls back to its tag duration rather than failing the job — including
one ffmpeg *hangs* on, which is why each file gets a timeout (a quarter of its own length,
floor five minutes): the measure pass is not the loop that watches for a cancel, so
without it one bad file holds the single worker until the process restarts. It does poll
the cancel flag between files, because on a long book this is minutes of decoding before
the progress bar moves at all.
`catalogue.edition_chapters` takes the measured durations as an override — the ABS API
never passes it, because the chapter list it serves must agree with the durations it
serves alongside.

### Metadata written into the m4b

Nothing in *this* app reads these tags — the ABS API serves title/author/narrator/series
from the database, and `scan_edition_audio` only takes duration and chapters. They exist
so the file is a good citizen anywhere else: played in another app, re-imported into ABS,
or found on disk in five years. The split below is measured, not assumed (see "Verified
live"): ffmpeg writes what its MP4 muxer maps, and a mutagen pass afterwards adds the rest.

| Atom | Source | Written by |
|---|---|---|
| `©nam` title | `book.title` | ffmpeg |
| `©ART` artist, `aART` album artist | `author.name` | ffmpeg |
| `©alb` album | `book.title` | ffmpeg |
| `©wrt` composer | `edition.narrator` (the m4b convention for narrator) | ffmpeg |
| `©gen` genre | `"Audiobook"` | ffmpeg |
| `©grp` grouping | `series.name` | ffmpeg |
| `trkn` track | `book.series_index` | ffmpeg |
| `stik` media type | `2` — marks it an **audiobook**, not music | ffmpeg |
| `pgap` gapless | `1` | ffmpeg |
| `©cmt` comment | provenance: transcoded from N MP3s by this app | ffmpeg |
| `desc` description | Hardcover URL when we have the slug | ffmpeg |
| `©mvn`/`©mvi`/`©mvc`/`shwm` | series name / index / count — **ffmpeg silently drops these** | mutagen |
| `covr` cover art | an image already in the folder (`cover.*`/`folder.*`, else the only image) | mutagen |
| `----:com.apple.iTunes:NARRATOR` | `edition.narrator`, for tools that look there | mutagen |

Doing cover art in mutagen rather than ffmpeg is not just tidiness: it keeps an image
stream out of a filter-graph output mapping, and it removes png/mjpeg
decoders/encoders/parsers from the minimal ffmpeg build entirely. The mutagen pass is
verified not to disturb the chapter track, the duration, or the `moov`-before-`mdat`
ordering that `+faststart` produced.

### Job model and worker

A `transcode_job` table (new Alembic revision on top of head `f8d2a63b7c14`) and **one**
background worker task started in `lifespan`, draining `queued` jobs oldest-first, one at a
time (ffmpeg is CPU-bound; a queue keeps the picture simple and the box usable).

```
transcode_job
  id, edition_id (FK→edition, index), user_id (FK→user, SET NULL)
  status        queued | running | done | failed | cancelled
  progress      0–100, null until running
  bitrate       what was actually used
  source_count  how many MP3s went in
  output_path   the resulting .m4b
  cancel_requested  bool — set by the request handler, polled by the worker
  error, created_at, started_at, finished_at
```

Cancellation goes through the row, not a shared process handle: the request handler sets
`cancel_requested`, the worker (already polling the progress pipe) kills ffmpeg and cleans
up the temp file. On **startup**, any row left `running` is marked `failed` ("interrupted
by a restart") and its stray `.part` file removed — never silently resumed.

### The destructive part, in order

1. Encode to `<folder>/.<name>.m4b.part` (leading dot + non-audio suffix: invisible to
   both our scanner and ABS).
2. **Validate**: `identify()` says family `mp4`, and its duration is within **1 second** of
   both what ffmpeg reported encoding and the summed *measured* durations. That tolerance
   can be this tight precisely because the offsets are measured rather than taken from
   tags: the only remaining difference is the AAC encoder's priming (21 ms on the test
   encode), which is constant and does not grow with the book. The one thing that widens
   it is a file that *couldn't* be measured and fell back to its tag duration: those
   seconds carry the +0.8% drift, so they buy 2% of **their own length** in extra
   tolerance — charged per estimated second, never against the whole book, so one
   unmeasurable file in twenty neither loosens the check on the other nineteen nor gets
   held to a measured file's flat second. Anything else fails the job
   and leaves the folder untouched. Validation comes **before** tagging, not after —
   mutagen cannot open a file that is not really an MP4, and its error is far less useful
   than ours (found by a test that fed the validator a stub's junk output).
3. **Tag** with mutagen (series movement atoms, cover art, narrator), then confirm the
   file still parses at the same duration — a tagging pass rewrites the container, and
   nothing should be deleted for a file that pass damaged.
3. Atomically rename to `<edition folder name>.m4b` (refusing an occupied name).
4. Delete the MP3s **and the chapter sidecars that were actually consumed** (the `.cue`
   that produced the chapters, or the whole per-disc set — never one we didn't use);
   prune emptied disc subfolders with
   `prune_empty_dirs`. Cover art, `.nfo`, and everything else stay. An ABS
   `metadata.json` stays too even when it *was* the chapter source
   (`sidecar_is_spent`): unlike a `.cue` it carries description, subtitle, series,
   narrator and tags that describe the book rather than the files being deleted, and
   nothing here can write them back.
5. `scan_edition_audio` rebuilds the `audio_file` rows and reads the chapters back out of
   the new file.
6. Job → `done`.

Any failure before step 3 leaves the edition exactly as it was.

### Effect on the ABS side

`MediaProgress.current_time` is absolute seconds over the whole edition and the M4B is the
same audio in the same order, so **progress and bookmarks stay valid**; the library item id
(`li_<edition.id>`) does not change. What does change: `audio_file` row ids (the ABS file
inos), so apps re-fetch the track list, and a client mid-playback on a track that just
vanished will error until it reloads. Total duration may shift by well under a second.
Read-state logic is untouched.

### UI

- `_edition_files.html`: a "Convert to M4B…" button under the table when `transcodable`;
  `_transcode_confirm.html` as the modal; `_transcode_status.html` as a progress panel that
  polls `GET /editions/{id}/transcode` every 2s while queued/running and swaps the refreshed
  file table in when the job finishes.
- Routes (`app/routes/transcode.py`): `POST /editions/{id}/transcode` (queue),
  `GET /editions/{id}/transcode` (status fragment), `POST /transcodes/{id}/cancel`,
  `POST /transcodes/{id}/dismiss`.
- **Activity page**: a "Transcoding" section for active jobs (book, progress, Cancel), and
  failed jobs join "Needs attention" with the error and Retry/Dismiss — the same convention
  as failed imports.

### Config, container, tests

```
TRANSCODE_BITRATE   # default 64k — target AAC bitrate; halved for mono sources,
                    # never above the source's own bitrate
FFMPEG_PATH         # default "ffmpeg" (ffprobe resolved alongside it)
```

Both optional, bare in `docker-compose.yml`, defaults in `app/config.py` only.

**Shipping ffmpeg — measured, not estimated** (docker, `python:3.12-slim` on trixie,
amd64; the app image today is 315 MB on disk / 104 MB pulled):

| how | on disk | pulled | build cost |
|---|---|---|---|
| `apt install --no-install-recommends ffmpeg` | **+434 MB** | +168 MB | none |
| `COPY --from=mwader/static-ffmpeg` (ffmpeg only) | **+129 MB** | +51 MB | none |
| **minimal source build** (chosen) | **+2 MB** | +1 MB | ~1.5 min, cached |

Confirmed on the finished image: 317 MB on disk / 105 MB pulled, against 315 / 104 before,
with a 2.04 MB `/usr/local/bin/ffmpeg` that runs the real conversion.

Debian's ffmpeg is one monolithic package whose dependency tail is 124 MB of libLLVM,
41 MB of libgallium and 27 MB of libz3 — the **Mesa 3D stack**, pulled in via
`libavdevice` → `libplacebo` → Vulkan/OpenCL. `/usr/bin/ffmpeg` itself is 1 MB. None of
it can be removed afterwards without breaking ffmpeg's dynamic linking. We convert MP3 to
AAC; paying 434 MB for a software GPU driver and a shader compiler is absurd, so the
Dockerfile gains a build stage compiling ffmpeg with everything disabled but what this
feature uses:

```
--disable-everything --disable-doc --disable-network --disable-autodetect
--disable-debug --disable-shared --enable-static --enable-small
--disable-ffplay --disable-ffprobe --disable-swscale --disable-postproc
--enable-decoder=mp3,mp3float,aac,aac_fixed,alac
--enable-encoder=aac,pcm_s16le
--enable-demuxer=mp3,mov,concat,ffmetadata
--enable-muxer=mp4,ipod,null
--enable-parser=mpegaudio,aac
--enable-protocol=file,pipe,concat,concatf
--enable-filter=aresample,aformat,concat,anull,atrim,aselect,anullsrc
--enable-bsf=aac_adtstoasc
```

That yields a **2.04 MB** static binary, copied into the runtime stage. **`ffmetadata` must
be in the demuxer list** — chapters are fed to ffmpeg as an input file, and without it the
whole command dies with "Invalid data found when processing input" (this is how the flag
list was found to be wrong the first time). The `null` muxer plus the `pcm_s16le` encoder
are what the duration-measuring pass decodes into; without the encoder ffmpeg answers
"Default encoder for format null (codec pcm_s16le) is probably disabled" (this is how the
list was found to be wrong the *second* time). No image codecs are needed because cover
art is written by mutagen, not ffmpeg. A missing flag fails loudly at encode time, never
silently. If the source build ever
becomes a maintenance annoyance, `COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg
/usr/local/bin/ffmpeg` is the one-line, zero-build fallback at +129 MB.

Tests split three ways. `tests/test_transcode.py` is the pure half: cue (both shapes,
frames, pre-gap, unresolvable FILE), chapters.txt, ffmetadata timebases, ABS metadata.json,
sidecar precedence and case-insensitive matching, the triviality demotion, chapter
normalisation and escaping, bitrate/layout selection, argv construction and progress
parsing. `tests/test_transcode_worker.py` drives `run_job` against a **stub ffmpeg that
writes a real MP4** whose `mvhd` duration matches what it claims to have encoded — so the
duration check is genuinely exercised, and `FAKE_SCALE` makes it lie to test the
truncated-encode rejection. It covers the eligibility guards (other audio present, a
mislabelled `.mp3`, a download in flight, an existing job, no ffmpeg), the happy path
(MP3s and the consumed cue gone, cover kept, disc folders pruned, audio rows rebuilt), and
every failure leaving the folder untouched: non-zero exit, unplayable output, truncated
output, missing folder, no stray temp files, cancel mid-encode, and
restart-with-a-running-row. `tests/test_transcode_routes.py` covers where the control
appears and where it deliberately does not, what the confirm dialog states, and that
queue/cancel/dismiss go through their guards.

### Verified live

The sandbox has no ffmpeg (`deb.debian.org` is blocked by the network policy) but Docker's
own network is not, so the encode was verified inside a container built from the minimal
configure line above. Three MP3s deliberately mismatched — 5s 44.1 kHz stereo, 7s
22.05 kHz **mono**, 4s 48 kHz stereo — plus an FFMETADATA chapter file, through the exact
command above:

- the `aformat` + `concat` filter chain handled the mixed rates and channel counts with no
  demuxer complaints, which is the reason for preferring it over the concat demuxer;
- `-progress pipe:1` emitted `out_time=00:00:16.000000` then `progress=end` — the progress
  reporting the job row needs;
- the output duration measured **16.023s against a 16.000s source sum**: +23 ms of encoder
  padding. Real, small, and exactly why validation step 2 is a tolerance (1% / 5s) rather
  than an equality check;
- `app/services/audio_format.identify()` reads it back as family `mp4`, mime `audio/mp4`,
  `has_video_track` False — so it survives our own importer's identification;
- `app/services/mp4_chapters.read_mp4_chapters()` returns all three chapters with correct
  titles and boundaries. ffmpeg's chapter text track is readable by the reader we already
  have — no new MP4 parsing needed. The last chapter comes back with `end: None`, which
  `scan_edition_audio` already closes with the track duration.

The tagging split was measured the same way. Feeding ffmpeg a full `-metadata` set and
dumping the result with mutagen showed it writes `©nam ©ART aART ©alb ©wrt ©gen ©grp
©cmt desc trkn stik pgap` — and **silently drops `movement`/`movement_index`**, so the
series atoms have no ffmpeg path at all. A mutagen pass then added `©mvn ©mvi ©mvc shwm`,
`covr` and the freeform narrator atom, after which the file still reported: same three
chapters, same 16.023s duration, and top-level box order still `ftyp moov free mdat` —
i.e. **`+faststart` survives the tagging pass** (ffmpeg leaves a `free` box that mutagen
writes into). The chapter side was checked too: an MP3 given real ID3v2 `CHAP`/`CTOC`
frames is read correctly by the *existing* `audio_meta._read_chapters`, which is what
makes source 1 above free; the same file's `TXXX:OverDrive MediaMarkers` frame is visible
to mutagen but ignored by our reader today, which is the gap source 2 closes.

The finished feature was then run end to end twice against that ffmpeg: once through
`run_job` directly (cue chapters chosen over trivial embedded ones, MP3s and the consumed
cue deleted, `cover.jpg` and `notes.nfo` kept, cover art and narrator embedded, audio rows
rebuilt around the single file), and once through the **running app over HTTP** — queue
from the file list, worker picks it up, queued → running → done, and the file list
replacing itself with the one m4b without a page reload.

What remains unverified is only the parts that need the user's own library: a real
multi-hour book (where encode time and the progress bar actually matter), a real `.cue`
from a torrent, and the ABS apps picking up the rebuilt file list.

## Future work (out of scope for this build)

- Public REST API: list books, download audio files, update reading state — designed to serve
  the companion app below (the internal routes should keep this in mind but v1 does not expose it).
- Android/iOS audiobook player companion app (similar to Smart Audiobook Player) that
  downloads from the library, plays audiobooks, and updates read status.
