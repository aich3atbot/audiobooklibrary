# Audiobook Library

Self-hosted audiobook manager, in a single container:

- **Syncs your book list from [Hardcover](https://hardcover.app)** — authors, series, covers,
  and read states (want to read / reading / read, with read dates). Two-way: changing a
  book's state in the UI pushes back to Hardcover.
- **Finds audiobook releases on [AudioBookBay](http://audiobookbay.fi)** and hands the magnet
  straight to your torrent client — [Deluge](https://deluge-torrent.org) today, others can be
  added.
- **Tracks the download** by asking the torrent client about it, and imports the finished
  audiobook into an [Audiobookshelf](https://www.audiobookshelf.org/)-style library:
  `Author/Series/{index} - Title/`.
- **Web UI** — library grid with filters, Hardcover search to add new books, release picker,
  activity page for downloads and import failures, settings page with connection checks.
- **User accounts** — login is mandatory (30-day session cookie). The `admin` account
  (password from `ADMIN_PASSWORD`) manages users; each user gets their own password and
  Hardcover token. *Limited* accounts are listeners: they sign in from Audiobookshelf apps
  only — no web UI, no Hardcover.

See `plan.md` for the full design.

## Requirements

- A [Hardcover](https://hardcover.app) account and API token
  (hardcover.app → Settings → Hardcover API).
- A running [Deluge](https://deluge-torrent.org) with its **web UI** enabled (`DOWNLOAD_URL`
  points at that, e.g. `http://host.docker.internal:8112`). Deluge authenticates on a
  password alone — `DOWNLOAD_USERNAME` is accepted but unused.
- Deluge must write completed downloads somewhere this app can see: mount **the directory it
  saves completed downloads to** as `/downloads`. If Deluge completes into
  `/downloads/complete`, that subdirectory is what to mount — not its parent.
- An `INDEX_URL` for AudioBookBay. Its mirrors rotate domains and some serve expired TLS
  certificates, so a plain `http://` URL is sometimes the working one.

## Run with Docker

```bash
cp .env.example .env    # fill in ADMIN_PASSWORD, INDEX_URL and DOWNLOAD_URL
# edit docker-compose.yml volume paths, then:
docker compose up --build
```

Open http://localhost:8000, log in as `admin` (password from `ADMIN_PASSWORD`) and create
user accounts — each with its own password and Hardcover token. The app syncs every user's
Hardcover library on startup and every `SYNC_INTERVAL_MINUTES` after that.

Each account is **full** or **limited**. A full account is the normal one: the web UI, its
own Hardcover token, searching and downloading. A **limited** account is for people who only
want to listen — it signs in from Audiobookshelf apps, plays anything in the library and
keeps its own progress and bookmarks, but cannot log in to the web UI, holds no Hardcover
token, and never changes anything on Hardcover. Switch an account between the two at any
time on the Users page (demoting clears its Hardcover token).

Volumes:

| Mount | Purpose |
|---|---|
| `/config` | SQLite database |
| `/downloads` | The directory Deluge writes *completed* downloads to |
| `/audiobooks` | The organized audiobook library (point Audiobookshelf here) |
| `/imports` | Optional: staging area for an existing collection (see below) |

By default imports **hardlink or copy** files so seeding torrents are left intact; set
`IMPORT_MODE=move` to relocate them instead. All settings are environment variables — see
`.env.example` for the full list.

The container runs as a non-root user — `PUID`/`PGID` from `.env`, default `1000:1000` —
so the config database and imported audiobooks are owned by that user on the host. The
mounted directories must be writable by it, and the completed downloads readable by it
(for hardlinks, ideally the torrent client runs as the same user). **Upgrading from a
version that ran as root:** fix ownership once before starting —

```bash
sudo chown -R 1000:1000 ./data/config ./data/audiobooks   # your CONFIG_DIR / LIBRARY_DIR
```

## Development

```bash
uv sync                      # install dependencies
cp .env.example .env         # then fill in tokens/paths
uv run alembic upgrade head  # create/migrate the database
uv run uvicorn app.main:asgi --reload
```

Run tests:

```bash
uv run pytest
```

External APIs are mocked in tests (respx); no tokens are needed to run them.

## How downloading works

1. Press **Download…** on a book → the app searches AudioBookBay for
   "{author} {title}" (falling back to the title alone) and shows a release picker with
   size, format and posting date. AudioBookBay publishes no seeder counts, so results keep
   the site's own ordering rather than a made-up ranking.
2. **Grab** reads the release's details page for the torrent's info hash, builds a magnet,
   and adds it to Deluge. The app records the hash.
3. The watcher asks Deluge about that hash every `WATCH_INTERVAL_SECONDS` — its progress is
   what the Activity page shows — and imports the download into `/audiobooks` as soon as
   Deluge reports the torrent finished. If Deluge can't be reached, it falls back to
   watching `/downloads` for a folder matching the release name that has stopped changing.
4. Anything that can't be matched or imported shows up on the **Activity** page with retry,
   manual-import (point it at a folder name in `/downloads`), and cancel actions — the app
   never guesses.

**Cancel & delete** on the Activity page removes the torrent *and its downloaded files* from
Deluge — which also ends any seeding of it — and then stops tracking the release. The UI asks
you to confirm first. If Deluge can't be reached, the release is still cancelled here and the
app tells you the torrent may still be running so you can remove it yourself.

## Audiobookshelf apps

The server speaks enough of the [Audiobookshelf](https://www.audiobookshelf.org/) API that
ABS client apps (the official Android/iOS app, and generally Plappa/ShelfPlayer) can
connect directly: add it as a server using the same URL as the web UI and log in with
a user account, full or limited (the admin account has no library and cannot log in to
apps). Imported books appear as an "Audiobooks" library with
covers, series, and chapters; streaming (with seeking), offline downloads, and listening
progress all work. Progress syncs across devices, and finishing a book in the app marks
it read on Hardcover automatically (for a limited account, which has no Hardcover, listening
stays local: progress and "finished" work exactly as they do for anyone else). Audio is
always direct-played (no transcoding) — m4b/m4a/mp3/flac/ogg all play natively in the apps.

## Importing an existing collection

Mount your current audiobook collection at `/imports` and open the **Imports** page. The
app scans it recursively (any folder directly containing audio files is one book;
`CD1`/`Disc 2` folders are grouped; loose `.m4b` files count individually), identifies
each entry by searching Hardcover (author/series/title from the folder names) with a
confidence badge, and lets you amend any match via library or Hardcover search.
Import rows one at a time, bulk-select, or import everything matched. Confirmed books are
**moved** out of `/imports` into the organized `/audiobooks` layout, and emptied folders
are cleaned up behind them. Imported books belong to no one until users' own Hardcover
libraries claim them: anyone who has the book on Hardcover sees it appear automatically,
and everyone else finds it as *available* in search.
