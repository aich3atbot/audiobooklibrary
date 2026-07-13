# Audiobook Library

Self-hosted audiobook manager: syncs your book list from [Hardcover](https://hardcover.app),
finds audiobook releases via [Prowlarr](https://prowlarr.com), tracks downloads, and organizes
finished audiobooks into an Audiobookshelf-style library.

See `plan.md` for the full design and roadmap.

## Development

```bash
uv sync                      # install dependencies
cp .env.example .env         # then fill in tokens/paths
uv run alembic upgrade head  # create/migrate the database
uv run uvicorn app.main:app --reload
```

Run tests:

```bash
uv run pytest
```

## Docker

```bash
docker compose up --build
```

Edit `docker-compose.yml` volume paths first: `/downloads` must match your download client's
completed-downloads directory, `/audiobooks` is the organized library output.
