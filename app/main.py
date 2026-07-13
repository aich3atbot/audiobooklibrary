import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.abs import library_routes as abs_library_routes
from app.abs import playback_routes as abs_playback_routes
from app.abs import routes as abs_routes
from app.abs.socket import wrap_asgi
from app.auth import RequireAuthMiddleware, resolve_session_secret
from app.config import get_settings
from app.routes import activity, admin, auth, downloads, imports, search, settings, ui
from app.services.audio_meta import audio_backfill_task
from app.services.importer import download_watch_loop
from app.services.sync import hardcover_sync_loop

logging.basicConfig(level=logging.INFO)

if not get_settings().admin_password:
    raise RuntimeError("ADMIN_PASSWORD must be set (user accounts are mandatory)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(hardcover_sync_loop()),
        asyncio.create_task(download_watch_loop()),
        asyncio.create_task(audio_backfill_task()),
    ]
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Audiobook Library", lifespan=lifespan)
# Middleware runs outermost-last-added: SessionMiddleware must wrap
# RequireAuthMiddleware so the session is available to the auth check.
app.add_middleware(RequireAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=resolve_session_secret(),
    same_site="lax",
    max_age=60 * 60 * 24 * 30,  # 30 days
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(ui.router)
app.include_router(search.router)
app.include_router(downloads.router)
app.include_router(activity.router)
app.include_router(settings.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(imports.router)
app.include_router(abs_routes.router)
app.include_router(abs_library_routes.router)
app.include_router(abs_playback_routes.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# socket.io shim for ABS clients wraps the FastAPI app; serve `asgi`.
asgi = wrap_asgi(app)
