# Audiobook Library

Self-hosted audiobook manager: syncs the user's book list from Hardcover, finds audiobook
releases via Prowlarr, tracks downloads, and organizes finished audiobooks into an
Audiobookshelf-style library folder.

**`plan.md` is the authoritative spec** — read it before making changes. It contains the full
architecture, data model, workflows, and milestones. Keep it updated as decisions change.

## Current state

Planning complete; no code yet. Implementation follows the milestones in plan.md
(1: skeleton → 7: polish). Check git log to see how far the build has progressed.

## Key decisions (do not silently revisit)

- **Stack**: Python 3.12, FastAPI + Jinja2 + HTMX (server-rendered, no SPA), SQLAlchemy 2.x +
  Alembic on SQLite, single container, uvicorn.
- **Background work**: asyncio tasks inside the FastAPI process (lifespan-managed). No
  Celery/Redis — do not introduce them.
- **Downloads**: the app never downloads directly. It grabs via Prowlarr
  (`POST /api/v1/search` with release `guid` + `indexerId`); Prowlarr's download client does
  the work, and the app watches `DOWNLOAD_DIR` for the finished files.
- **Library layout**: `Author/Series/{SeriesIndex} - Title/`, or `Author/Title/` when there is
  no series. Sanitize filesystem-unsafe characters.
- **Read state**: two-way sync, but **Hardcover is the source of truth** — push local changes
  first, then pull; Hardcover wins conflicts. Book identity is anchored on the Hardcover
  *book* id (editions collapsed).
- **Single user, no app auth** in v1 (assumed trusted LAN / reverse proxy).
- **Config via env vars** only: `HARDCOVER_TOKEN`, `PROWLARR_URL`, `PROWLARR_API_KEY`,
  `DOWNLOAD_DIR`, `LIBRARY_DIR`, `CONFIG_DIR`, `SYNC_INTERVAL_MINUTES`.

## External API gotchas

- **Hardcover** (`https://api.hardcover.app/v1/graphql`, bearer token): the API is beta and
  the schema shifts. Verify field/query names against the live API (introspection with the
  user's token) before writing or changing queries — do not trust remembered schema.
- **Prowlarr**: audiobook searches use category `3030`. A "grab" is a POST back to the search
  endpoint, not a separate endpoint.

## Conventions

- Project layout, data model, and state enums (`read_state`, `download_state`) are defined in
  plan.md — follow them exactly so UI, sync, and importer stay consistent.
- Import failures must surface in the Activity page for manual review — never guess a match.
- Tests: pytest; mock HTTP with respx; importer tests use tmp dirs with fake download layouts.
- Each milestone must end in a working, runnable state.
