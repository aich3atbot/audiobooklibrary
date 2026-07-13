# Audiobook Library

Self-hosted audiobook manager, in a single container:

- **Syncs your book list from [Hardcover](https://hardcover.app)** — authors, series, covers,
  and read states (want to read / reading / read, with read dates). Two-way: changing a
  book's state in the UI pushes back to Hardcover.
- **Finds audiobook releases via [Prowlarr](https://prowlarr.com)** and grabs them through
  Prowlarr's configured download client.
- **Watches the download directory** and imports finished audiobooks into an
  [Audiobookshelf](https://www.audiobookshelf.org/)-style library:
  `Author/Series/{index} - Title/`.
- **Web UI** — library grid with filters, Hardcover search to add new books, release picker,
  activity page for downloads and import failures, settings page with connection checks.
- **Optional login** — set `AUTH_USERNAME` and `AUTH_PASSWORD` to require a single-user
  login (30-day session cookie); leave them empty to run open on a trusted LAN.

See `plan.md` for the full design.

## Requirements

- A [Hardcover](https://hardcover.app) account and API token
  (hardcover.app → Settings → Hardcover API).
- A running [Prowlarr](https://prowlarr.com) with at least one indexer and a download client
  configured. The download client must write completed downloads to a directory this app can
  see (the `/downloads` volume).

## Run with Docker

```bash
cp .env.example .env    # fill in HARDCOVER_TOKEN and PROWLARR_API_KEY
# edit docker-compose.yml volume paths, then:
docker compose up --build
```

Open http://localhost:8000. The app syncs your Hardcover library on startup and every
`SYNC_INTERVAL_MINUTES` after that.

Volumes:

| Mount | Purpose |
|---|---|
| `/config` | SQLite database |
| `/downloads` | Your download client's completed-downloads directory |
| `/audiobooks` | The organized audiobook library (point Audiobookshelf here) |
| `/imports` | Optional: staging area for an existing collection (see below) |

By default imports **hardlink or copy** files so seeding torrents are left intact; set
`IMPORT_MODE=move` to relocate them instead. All settings are environment variables — see
`.env.example` for the full list.

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

1. Press **Download…** on a book → the app searches Prowlarr (audiobook category) and shows
   a release picker sorted by seeders.
2. **Grab** hands the release to Prowlarr, which forwards it to your download client. The
   app records what it grabbed.
3. The watcher polls `/downloads`, matches finished downloads to grabbed releases by name,
   waits until the files stop changing, then imports them into `/audiobooks`.
4. Anything that can't be matched or imported shows up on the **Activity** page with retry,
   manual-import (point it at a folder name in `/downloads`), and cancel actions — the app
   never guesses.

## Audiobookshelf apps

The server speaks enough of the [Audiobookshelf](https://www.audiobookshelf.org/) API that
ABS client apps (the official Android/iOS app, and generally Plappa/ShelfPlayer) can
connect directly: add it as a server using the same URL as the web UI and log in with
`AUTH_USERNAME`/`AUTH_PASSWORD`. Imported books appear as an "Audiobooks" library with
covers, series, and chapters; streaming (with seeking), offline downloads, and listening
progress all work. Progress syncs across devices, and finishing a book in the app marks
it read on Hardcover automatically. Audio is always direct-played (no transcoding) —
m4b/m4a/mp3/flac/ogg all play natively in the apps.

## Importing an existing collection

Mount your current audiobook collection at `/imports` and open the **Imports** page. The
app scans it recursively (any folder directly containing audio files is one book;
`CD1`/`Disc 2` folders are grouped; loose `.m4b` files count individually), suggests a
matching book from your Hardcover library with a confidence badge, and lets you amend any
match — including searching Hardcover and adding the book to your shelf on the spot.
Import rows one at a time, bulk-select, or import everything matched. Confirmed books are
**moved** out of `/imports` into the organized `/audiobooks` layout, and emptied folders
are cleaned up behind them.
