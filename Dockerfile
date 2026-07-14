FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY app/ ./app/
RUN uv sync --frozen --no-dev

ENV PATH="/opt/venv/bin:$PATH"

VOLUME ["/config", "/downloads", "/audiobooks"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# app.main:asgi = FastAPI wrapped with the socket.io shim for ABS clients
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:asgi --host 0.0.0.0 --port 8000"]
