# Audiobook Library

Self-hosted audiobook manager: syncs the user's book list from Hardcover, finds audiobook
releases on a torrent indexer (AudioBookBay), downloads them with a torrent client
(Deluge), and organizes finished audiobooks into an Audiobookshelf-style library folder.

**`plan.md` is the authoritative spec** — read it before making changes. It contains the full
architecture, data model, workflows, and milestones. Keep it updated as decisions change.

## Current state

All seven plan.md milestones plus collection import (/imports), an
Audiobookshelf-compatible API, the direct torrent pipeline (AudioBookBay + Deluge, which
replaced Prowlarr), the **multi-user conversion** (mandatory accounts, virtual admin,
per-user Hardcover sync and ABS progress over a shared /audiobooks store — see plan.md
"Multi-user conversion"), and **multi-edition support** (a book can hold several
recordings as `edition` rows — see plan.md "Multi-edition support") are built, tested,
and committed. The Alembic history is a **single squashed revision**, `4279694b0300`
(creates the whole current schema; no upgrade path from any earlier revision — the only
live database, `data/config/`, was stamped at it by hand). Run `uv run pytest` (all external APIs are
mocked with respx). Awaiting real-world verification: the ABS API with the official app
(per user account; note item ids changed to `li_<edition.id>`), one real UI grab →
download → import through the user's own Deluge, the admin/imports flows, and the new
add-another-edition flow in the browser.

## ABS-compatible API

Lives in `app/abs/`. **docs/abs-api-contract.md pins the exact protocol shapes, verified
from the ABS server/app source — do not code ABS endpoints from memory; check the doc,
and re-verify against source when extending.** JWTs share the UI session secret; /login
dispatches JSON (ABS) vs form (UI) by content type. The served entrypoint is
`app.main:asgi` (FastAPI wrapped in a socket.io shim), not `app.main:app`. A library
item is one *edition* (`li_<edition.id>`); only editions with `library_path` set are
exposed, progress/bookmarks are per edition, and multi-edition books carry their label
in the item title. ABS logins are user accounts (the virtual admin is rejected); token
`userId` is the account's stable uuid. Finishing any edition in an app marks the book
read on Hardcover via `update_read_state`. **Not every ABS endpoint is authenticated**:
covers (`/api/items/:id/cover`), author photos (`/api/authors/:id/image`, a 302 to
Hardcover's `author.image_url`) and direct-play streaming
(`/public/session/:id/track/:index`) take no token — the apps send none, gated on the
`serverVersion` we advertise. They live in `app/abs/public_routes.py` and `/public/` is
open in `RequireAuthMiddleware`; putting either behind auth breaks covers and playback.
**Chapters**: mutagen reads ID3 `CHAP` (mp3) but exposes *nothing* for MP4, so
`app/services/mp4_chapters.py` parses the container itself — Nero `moov/udta/chpl` first,
then a QuickTime chapter text track via the audio track's `tref/chap`. Files with no
embedded chapters fall back to one chapter per track, and a book's chapters are its
files' chapters shifted by each file's start offset. Improving extraction means bumping
`CHAPTER_SCAN_VERSION` in `app/services/audio_meta.py`, which triggers a one-time
re-scan of already-scanned MP4s at startup (marker in `app_state`).

Third-party clients (Lissen) are supported too, and they exercise paths the official app
never touches: item detail **without** `expanded=1` (must return the full item, not the
minified list shape), `?filter=<group>.<base64>` on the items list, the author landing
page, and `POST /api/items/batch/get`.

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
  no series. Sanitize filesystem-unsafe characters. A labelled edition suffixes the
  *series* folder (`Author/Series {Label}/{idx} - Title/`) or, standalone, the book
  folder; only the unlabelled edition uses the plain path, and once a book has two
  editions all of them are labelled. Relabelling moves the folder immediately
  (`relabel_edition` — filesystem first, DB second). See plan.md "Multi-edition
  support"; do not revisit the layout silently.
- **Audio is identified by contents, not extension** (`app/services/audio_format.py`,
  `identify`/`has_video_track`/`corrected_name`). mutagen alone is not enough: its sniffing
  scores on the *filename*, so `mutagen.File()` returns None for a mislabelled raw AAC
  stream, and `MP4Info.load` falls back to the `mvhd` duration when a container has no
  `soun` track — a video-only MP4 parses as if it were audio. So the MP4 track handlers are
  read from the container directly (reusing the `mp4_chapters` box reader). Identification
  **only rules files out** — a file mutagen cannot parse still imports under its own name;
  only positive video, or an unconfirmable `.mp4`, is dropped. Contradicted extensions are
  renamed by *family* (`.m4b`/`.m4a`/`.mp4` are one family), so `.m4b` is never rewritten
  to `.m4a`. Parse audio through `identify`, not bare `mutagen.File`.
- **Import mode**: default is hardlink-or-copy (seeding torrents keep their files);
  `IMPORT_MODE=move` opts into relocating. Do not change the default back to move.
  Seeding also survives import by default: `DOWNLOAD_REMOVE_IMMEDIATELY=true` opts into
  removing the torrent + data from the client right after a successful import (a failed
  removal never un-imports; it is noted on the release for the Activity page).
- **Collection import** (`/imports` volume, Imports page): hard-coded path, no env var
  (`Settings.imports_dir` exists only for tests). Entries are identified by **searching
  Hardcover** (folder-name heuristics; cached in app_state per entry), never by shelving —
  imported books are **ownerless** until each user's own sync attaches their shelf entry.
  Always MOVES files (draining /imports is the point) regardless of IMPORT_MODE, which
  applies to the download pipeline only. It moves file-by-file through the shared
  `collect_files(keep_unknown=True)`, so audio gets the same content identification as a
  download; a rejected file is **left in /imports, never deleted** (a move would destroy
  the user's only copy) and its surviving folder is the signal that the entry did not
  drain. Emptied disc subfolders are pruned so the entry still cleans up. A successful batch kicks a background all-user
  sync. An entry matched to an already-available book imports as an additional edition;
  unlabelled imports into available books refuse with guidance. Every matched row can
  **choose the edition** in one lazily loaded fold (`_import_editions.html`, fed by
  `edition_sections`): own label, series labels, this book's editions (replace), then
  Hardcover's. Fields are namespaced by the entry's rel path (`ns_field`) — the bulk
  Import buttons post the whole table, so unprefixed names would cross-wire the rows.
  The selected radio decides the label (`edition_choice(pick_wins=True)`; the download
  pickers keep the opposite, override-box, semantics). Picking a "replace" option
  **deletes** that edition's files (`import_entry(replace_edition_id=...)`) and confirms
  at selection time, not at import time, because a bulk import must not fire one confirm
  per row. Duration/narrator matches are advisory badges; **never preselect an edition**.
- **Read state**: per user (`user_book` rows over shared `book` metadata), two-way sync with
  each user's own Hardcover token, but **Hardcover is the source of truth** — push local
  changes first, then pull; Hardcover wins conflicts. Book identity is anchored on the
  Hardcover *book* id (sync collapses editions to the canonical book); downloaded
  recordings are per-book `edition` rows (unique book+label, optional Hardcover edition
  id). Download state is shared and per edition; a book displays the aggregate (not
  present / downloading / available — `book_status`). A book one user made available
  cannot be grabbed again — search offers "add to my library", and the book detail page
  (`/books/{id}`, reached from a card's cover/title/available badge and from Activity)
  offers "Download another edition" (per-edition guard). Read state stays book-level:
  listening to any edition moves the book.
- **Listening drives read state** (`app/abs/progress.py`, every progress route funnels
  through `apply_progress`): past `MARK_READING_AFTER_MINUTES` (default 1) the book becomes
  *currently reading* dated today (`user_book.started_at`, pushed as Hardcover's
  `first_started_reading_date`); finished marks it *read* dated today; and because the
  trailing credits usually go unplayed, a book left within `MARK_READ_TAIL_MINUTES`
  (default 30) of the end is marked finished and read as soon as progress arrives for a
  **different** book (another edition of the same book doesn't count). Both are plain
  durations — **there is no percentage-of-book rule; do not reintroduce one.** The single
  exception is the tail being capped at half the duration, so a 30-min tail can't make a
  20-min book readable from its first second. Either setting may be 0 (mark reading the
  moment playback starts / only a complete listen counts). A finished row resynced at
  least `RELISTEN_MIN_POSITION` (60s, fixed — *not* the reading threshold, which may be 0)
  in but before the near-finish mark is treated as a re-listen and un-finished — the only
  thing that clears `is_finished` on its own, so keep the window narrow: a stray
  `currentTime: 0` or a rewind into the last chapter must not trip it. The ABS progress handlers are
  `async def`, so every `apply_progress` call goes through `run_in_threadpool` — the
  Hardcover push it can trigger has a 30s timeout and would otherwise stall the event
  loop, and streaming rides that same loop. Keep it off the loop.
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
  **hardcover.app (the site) routes on slugs, not ids**, so outbound links need
  `book.hardcover_slug` / `series.hardcover_slug`; `slug` rides along on the queries we
  already make, and `backfill_hardcover_slugs` tops up rows stored before that. The site
  itself is *not* reachable from the sandbox (only `api.hardcover.app` is), so link urls
  can't be verified by fetching them.
- **AudioBookBay** (HTML scraping, no API — contract pinned in plan.md, verified live):
  a **browser User-Agent is mandatory** (ABB blocks tool UAs); an exhausted search page
  returns 200 with zero posts, not a 404; post metadata sits in inline `<span>`s split by
  `<br>`, so parse element *text*, not raw HTML; some mirrors base64-encode posts
  (`div.post.re-ab`). No seeder counts exist — don't invent a "best match" ranking. Mirrors
  rotate domains and some have expired TLS certs, so `INDEX_URL` may be plain `http://`.
  **Releases lie about file extensions** — one shipped a 22-hour audiobook named `.mp4`
  whose bytes are a raw ADTS AAC stream. Never trust a suffix; see `audio_format.identify`.
- **Deluge** (web UI JSON-RPC at `{DOWNLOAD_URL}/json`, verified against 2.2.0): auth is
  `auth.login([password])`, password-only — an **empty password is valid**, so never gate a
  connection check on it. **`is_finished` is the completion signal, never the `state`
  string** (finished torrents commonly sit in state "Queued"). `save_path` is in Deluge's
  filesystem namespace, so find downloads by the torrent's `name` inside `DOWNLOAD_DIR`.
  `daemon.info` does not exist on 2.2.0 — use `daemon.get_version`.
- **qBittorrent** (Web UI API at `{DOWNLOAD_URL}/api/v2`, verified against 5.2.3 / API
  2.15.1): login answers **204**, not the documented 200 "Ok."; expired sessions answer 403
  (re-login once and retry). `torrents/add` answers JSON with `added_torrent_ids` and a
  duplicate add is a **409** (treat as success); `category=` on add auto-creates the
  category. In `torrents/info`, `progress` is 0..1 and **`amount_left` is 0 for a
  metadata-less torrent — completion is `progress >= 1.0`, never `amount_left == 0`**;
  `total_size` is -1 before metadata arrives. `name` is the *display* name — a magnet's
  `dn` sticks even after metadata arrives, so it can differ from the on-disk folder;
  use **`root_path`**'s basename to locate the download, falling back to `content_path`
  only when `root_path` is empty (a torrent with no root folder). `content_path` is a
  trap: for a torrent holding exactly one file it points at the *file* even when that
  file lives inside a root folder, so its basename is the .m4b rather than the folder
  the importer has to find.

## Conventions

- Project layout, data model, and state enums (`read_state`, `download_state`) are defined in
  plan.md — follow them exactly so UI, sync, and importer stay consistent.
- Import failures must surface in the Activity page for manual review — never guess a match.
- Tests: pytest; mock HTTP with respx; importer tests use tmp dirs with fake download layouts.
- Each milestone must end in a working, runnable state.
