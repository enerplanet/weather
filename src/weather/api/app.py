"""Thin HTTP interface over weather.get_point_weather().

Built for one specific gap: buem's production deployment cannot reach the
processed archives on this repo's data host directly (VPN-protected
university network; a request-serving container can't join a human VPN
client the way a developer does). This exposes exactly one operation --
(provider, latitude, longitude, year) -> hourly T/GHI/DHI/DNI -- over HTTP,
so a service running on/near the data host can sit inside the network
boundary while callers outside it never need filesystem or bulk access.

Deliberately narrow: no file listing, no bulk/archive download, nothing
beyond the single point-query already at the heart of weather's own public
API (weather.get_point_weather). That's the whole point -- a security
review of this surface should be much smaller than a review of "open
network access to the data host".

Scope note: this is a scaffold. Not wired into this repo's own CI/packaging
defaults (opt-in via the `api` extra, started explicitly with
`weather serve`), not deployed, not committed/pushed without review. See
README.md in this directory.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=int(os.environ.get("WEATHER_API_CACHE_SIZE", "256")))
def _cached_point_weather(provider: str, latitude: float, longitude: float, year: int):
    """Cached wrapper over ``get_point_weather`` -- safe to cache forever.

    Unlike most caches, this one has no staleness problem: a processed
    archive for (provider, latitude, longitude, year) is immutable once
    built (barring a deliberate pipeline re-run, which is a rare, manual
    event, not something this cache needs to detect). No TTL, no
    invalidation logic -- just reuse by key for the life of the process.

    Exists because the underlying call is genuinely expensive per request
    -- opening the archive file(s) plus live DISC/DIRINT DNI/DHI
    reconstruction over the full timeseries, not a cheap lookup (see
    point_query.py's own module docstring) -- and multiple consumers
    (buem-gateway today, other tech-specific gateways later) querying the
    same location/year would otherwise each pay that cost independently.

    In-memory and per-process, same caveat as the rate limiter below: correct
    for a single dev-server process, not for a multi-worker production
    deployment (would need a shared cache, e.g. Redis, at that point --
    `WEATHER_API_CACHE_SIZE` only bounds one worker's own cache).

    A failed lookup (``FileNotFoundError`` etc.) is never cached --
    ``lru_cache`` only caches a function's return value, not an exception
    it raised, so a location with no archive yet keeps retrying on every
    call rather than caching the failure.
    """
    from weather import get_point_weather

    return get_point_weather(latitude, longitude, year, provider=provider)

_PROVIDER_OUTPUT_DIR_GETTERS = {
    "merra-2": "merra2_output_dir",
    "cosmo-rea6": "cosmo_output_dir",
    "era5-land": "era5_output_dir",
}


def _valid_api_keys() -> set[str]:
    """Static API keys from WEATHER_API_KEYS (comma-separated).

    A minimum viable control, not a substitute for network-level
    restrictions (firewall / IP allowlist) -- see README.md.
    """
    raw = os.environ.get("WEATHER_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


class _RateLimiter:
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


def _available_years(provider: str) -> list[int]:
    """Years with a processed archive for *provider*, inferred from
    filenames already on disk.

    Deliberately returns only the derived year list, not a directory
    listing -- keeps /v1/health from becoming a bulk-discovery endpoint.
    """
    from weather.settings import EnvSettings

    attr = _PROVIDER_OUTPUT_DIR_GETTERS.get(provider)
    if attr is None:
        return []
    output_dir: Path = getattr(EnvSettings, attr)()
    if not output_dir.is_dir():
        return []

    years: set[int] = set()
    for path in output_dir.glob("*.nc"):
        # e.g. MERRA2_2018_01_all_attrs.nc / COSMO_REA6_2018_01_all_attrs.nc
        for part in path.stem.split("_"):
            if part.isdigit() and len(part) == 4:
                years.add(int(part))
                break
    return sorted(years)


def create_app() -> Flask:
    app = Flask(__name__)
    limiter = _RateLimiter(
        max_requests=int(os.environ.get("WEATHER_API_RATE_LIMIT", "60")),
        window_seconds=60.0,
    )

    @app.before_request
    def _authenticate() -> Any:
        valid_keys = _valid_api_keys()
        if not valid_keys:
            logger.error(
                "WEATHER_API_KEYS is unset/empty -- refusing all requests."
            )
            return jsonify(error="server misconfigured: no API keys set"), 503

        key = request.headers.get("X-API-Key", "")
        if key not in valid_keys:
            logger.warning(
                "Rejected request from %s: bad/missing API key.",
                request.remote_addr,
            )
            return jsonify(error="invalid or missing X-API-Key"), 401

        if not limiter.allow(key):
            return jsonify(error="rate limit exceeded"), 429

        return None

    @app.after_request
    def _audit_log(response: Response) -> Response:
        logger.info(
            "%s %s -> %s (key=%s..., addr=%s)",
            request.method,
            request.full_path,
            response.status_code,
            request.headers.get("X-API-Key", "")[:8],
            request.remote_addr,
        )
        return response

    @app.get("/v1/health")
    def health() -> Any:
        from weather.registry import list_providers

        providers: dict[str, dict[str, Any]] = {}
        for name in list_providers():
            try:
                providers[name] = {"years": _available_years(name)}
            except OSError as exc:
                providers[name] = {"error": str(exc)}
        return jsonify(status="ok", providers=providers)

    @app.get("/v1/weather/point")
    def weather_point() -> Any:
        provider = request.args.get("provider", "")
        try:
            latitude = float(request.args["lat"])
            longitude = float(request.args["lon"])
            year = int(request.args["year"])
        except (KeyError, ValueError):
            return jsonify(
                error="lat, lon, year are required and must be numeric"
            ), 400
        if not provider:
            return jsonify(error="provider is required"), 400

        try:
            df = _cached_point_weather(provider, latitude, longitude, year)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except (RuntimeError, KeyError) as exc:
            # point_query.py raises these when the archive itself isn't
            # in a servable state for this query -- an unrepaired
            # ERA5-Land boundary month (RuntimeError) or a file that
            # predates the lat/lon-retention convention (KeyError).
            # Both are real, actionable server-side data problems, not
            # a bad request or a crash -- surface them as such instead
            # of falling through to Flask's generic unhelpful 500.
            return jsonify(error=str(exc)), 503

        buf = io.BytesIO()
        df.reset_index().to_parquet(buf, index=False)
        return Response(buf.getvalue(), mimetype="application/octet-stream")

    return app
