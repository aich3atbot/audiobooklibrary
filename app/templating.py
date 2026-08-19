from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import book_failed, book_status, display_status, format_series_index


def human_size(n: int | float | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def human_duration(seconds: float | int | None) -> str:
    """`9h 12m`, `47m`, `38s` — long enough to be useful, short enough to sit
    inline in a sentence."""
    if not seconds:
        return "?"
    total = int(seconds)
    hours, minutes = divmod(total // 60, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{total}s"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["human_size"] = human_size
templates.env.filters["duration"] = human_duration
templates.env.filters["series_index"] = format_series_index
templates.env.globals["display_status"] = display_status
templates.env.globals["book_status"] = book_status
templates.env.globals["book_failed"] = book_failed
# Callable, not a value: settings are cached and tests monkeypatch them.
templates.env.globals["downloads_enabled"] = lambda: get_settings().downloads_enabled
