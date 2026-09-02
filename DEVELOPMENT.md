# Development Guide

See `plan.md` for the full design.

## Run with Docker

```bash
cp .env.example .env    # fill in ADMIN_PASSWORD, the four paths, INDEX_URL and DOWNLOAD_URL
# everything else in it is commented out — uncomment only what you want to change, then:
docker compose up --build
```

Open http://localhost:8000, log in as `admin` (password from `ADMIN_PASSWORD`) and create
user accounts — each with its own password and Hardcover token. The app syncs every user's
Hardcover library on startup and every `SYNC_INTERVAL_MINUTES` after that.

`ADMIN_PASSWORD` creates the admin account on first start and is checked at every start
after that: change it and the stored password changes with it (signing the admin out
everywhere), remove it from `.env` and the stored one stands. It is the only way to set
that password, so put it back if you ever forget it.

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
| `/downloads` | The directory your torrent client writes *completed* downloads to |
| `/audiobooks` | The organized audiobook library (point Audiobookshelf here) |
| `/imports` | Optional: staging area for an existing collection (see below) |

The app's settings are environment variables. **README's *Environment* section is the
reference** — it documents every one with its default, grouped as accounts, downloading,
Hardcover sync, listening and read state, and converting to M4B; `.env.example` lists the
same set as a file to copy. Don't restate them here: defaults live in `app/config.py` and
nowhere else, and a second copy of the table is a second thing to get wrong.

Only `ADMIN_PASSWORD` (on a fresh install) and the four host paths are required. Leave any
other variable out of `.env` and the app applies its own default — `docker-compose.yml`
lists the optional ones bare, so an unset one never reaches the container at all.

Two that catch people out:

- `DOWNLOAD_IMPORT_MODE` (default `copy`) governs **downloads only**, as its prefix says: a
  finished download is hardlinked (or copied) so the torrent keeps its files and goes on
  seeding, and `move` relocates them instead. Importing from `/imports` always *moves*,
  whatever this is set to — draining that directory is the point of it.
- A blank value is treated as absent, *except* where the field's own default is `""` — there
  empty is meaningful. No `DOWNLOAD_CLIENT` disables downloading, no `DOWNLOAD_LABEL` means
  no label, and a blank `ADMIN_PASSWORD` still refuses to start.

The four paths are the exception to "the app's settings". `CONFIG_DIR`, `DOWNLOAD_DIR`,
`LIBRARY_DIR` and `IMPORTS_DIR` are read by **docker-compose itself**, which uses them as
the *host* side of each bind mount; the container's own paths are the fixed mount points in
the table above, and compose never passes these names into it. `app/config.py` happens to
have same-named fields holding the container side, so a value here reaches the app only when
you run it outside a container (see below) — never add them to the container's
`environment:`, where overriding a field would just point the app outside its own volume.

The container runs as a non-root user — `PUID`/`PGID` from `.env`, default `1000:1000` —
so the config database and imported audiobooks are owned by that user on the host. The
mounted directories must be writable by it, and the completed downloads readable by it
(for hardlinks, ideally the torrent client runs as the same user). **Upgrading from a
version that ran as root:** fix ownership once before starting —

```bash
sudo chown -R 1000:1000 ./data/config ./data/audiobooks   # the dirs mounted as /config and /audiobooks
```

## Local Development

```bash
uv sync                      # install dependencies
cp .env.example .env         # then fill in tokens/paths
uv run alembic upgrade head  # create/migrate the database
uv run uvicorn app.main:asgi --reload
```

The entrypoint is `app.main:asgi` (the FastAPI app wrapped in a socket.io shim), not
`app.main:app`.

This is the one context where the path variables configure the *app*: pydantic-settings
reads `.env` directly, so `CONFIG_DIR=./data/config` really does put the database there
instead of in `/config`. That is the same `.env` compose uses for its bind-mount sources, so
one file serves both — the values just mean different sides of the mount depending on how
you start the app.

Run tests:

```bash
uv run pytest
```

External APIs are mocked in tests (respx); no tokens are needed to run them.
