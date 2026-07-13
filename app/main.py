from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import ui


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background tasks (Hardcover sync, download watcher, importer) start here
    # in later milestones.
    yield


app = FastAPI(title="Audiobook Library", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(ui.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
