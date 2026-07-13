import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import activity, downloads, search, ui
from app.services.importer import download_watch_loop
from app.services.sync import hardcover_sync_loop

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(hardcover_sync_loop()),
        asyncio.create_task(download_watch_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Audiobook Library", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(ui.router)
app.include_router(search.router)
app.include_router(downloads.router)
app.include_router(activity.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
