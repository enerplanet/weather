"""Per-key fixed-window rate limiting for weather serve."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from flask import Response, request


class RateLimiter:
    """Minimal fixed-window limiter, per API key.

    In-memory only -- correct for a single-process dev server; a
    multi-worker WSGI deployment would need a shared store (e.g. redis)
    instead. Hand-rolled deliberately, to avoid adding flask-limiter as a
    new dependency for what is currently a scaffold.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


def make_rate_limit_headers(limiter: RateLimiter) -> Callable[[Response], Response]:
    """Build the after_request hook that reports rate-limit state."""

    def _rate_limit_headers(response: Response) -> Response:
        # Reads RateLimiter's existing state directly rather than adding
        # methods to the class -- its internals are out of scope for this
        # change. allow() already ran once in _authenticate() for this
        # request (or never, if auth failed first -- see the note below),
        # so this never double-counts a hit.
        key = request.headers.get("X-API-Key", "")
        hits = limiter._hits.get(key)
        remaining = max(0, limiter.max_requests - (len(hits) if hits else 0))
        if hits:
            elapsed = time.monotonic() - hits[0]
            reset = max(0, int(limiter.window_seconds - elapsed) + 1)
        else:
            reset = int(limiter.window_seconds)

        response.headers["RateLimit-Limit"] = str(limiter.max_requests)
        response.headers["RateLimit-Remaining"] = str(remaining)
        response.headers["RateLimit-Reset"] = str(reset)
        if response.status_code == 429:
            response.headers["Retry-After"] = str(reset)
        # ponytail: on the 401/503 pre-auth-fail paths, allow() was never
        # called this request, so `hits` reflects the last successful
        # request's pruning, not a fresh one -- best-effort, not exact,
        # on those two paths specifically. Exact would need a read method
        # added inside RateLimiter, out of scope for this change.
        return response

    return _rate_limit_headers
