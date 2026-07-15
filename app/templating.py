from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import display_status


def human_size(n: int | float | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["human_size"] = human_size
templates.env.globals["display_status"] = display_status
# Callable, not a value: settings are cached and tests monkeypatch them.
templates.env.globals["downloads_enabled"] = lambda: get_settings().downloads_enabled
