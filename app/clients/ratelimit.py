"""Back off from a service that is rate limiting us.

When a service answers with a rate limit, the very next request would be
limited too (and repeated hammering is how a scraper gets its IP banned), so
the client records a cooldown and refuses to send anything to that service
until it has passed. The cooldowns live in process memory: the app is one
process, and a restart forgetting them costs at most one more limited request.

What counts as a rate limit is deliberately defensive — neither Hardcover's
nor AudioBookBay's limited response has been seen live (provoking one would
risk exactly the ban this avoids): HTTP 429, or 503 that carries a
Retry-After. A 503 without one is an ordinary outage, and a 403 is never a
rate limit (AudioBookBay answers 403 to a blocked User-Agent, and the dev
sandbox's network policy answers 403 too).
"""

import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

DEFAULT_COOLDOWN = 60.0
MAX_COOLDOWN = 3600.0

_lock = threading.Lock()
_until: dict[str, float] = {}


class RateLimited(Exception):
    """A service is limiting us; `retry_after` is the seconds left to wait."""

    def __init__(self, service: str, retry_after: float):
        self.service = service
        self.retry_after = retry_after
        super().__init__(f"{service} is rate limiting requests; retry in {self.wait_seconds}s")

    @property
    def wait_seconds(self) -> int:
        return max(1, int(self.retry_after + 0.999))


def check(key: str) -> float | None:
    """Seconds left on the cooldown for `key`, or None when requests may go."""
    with _lock:
        until = _until.get(key)
        if until is None:
            return None
        left = until - time.monotonic()
        if left <= 0:
            del _until[key]
            return None
        return left


def trip(key: str, seconds: float) -> float:
    """Start (or extend) the cooldown for `key`; returns the seconds applied."""
    seconds = min(max(seconds, 1.0), MAX_COOLDOWN)
    with _lock:
        until = time.monotonic() + seconds
        _until[key] = max(until, _until.get(key, 0.0))
    return seconds


def reset() -> None:
    """Forget every cooldown (tests)."""
    with _lock:
        _until.clear()


def retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or an HTTP date), or
    None when it is absent or unreadable."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def limited_for(response: httpx.Response) -> float | None:
    """The cooldown a response asks for when it is a rate limit, else None."""
    wait = retry_after(response)
    if response.status_code == 429:
        return wait if wait is not None else DEFAULT_COOLDOWN
    if response.status_code == 503 and wait is not None:
        return wait
    return None
