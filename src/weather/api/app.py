"""Thin HTTP interface over weather.get_point_weather().

Built for one specific gap: buem's production deployment cannot reach the
processed archives on this repo's data host directly (VPN-protected
university network; a request-serving container can't join a human VPN
client the way a developer does). This exposes exactly one operation --
(provider, latitude, longitude, year) -> hourly weather for the requested
variables -- over HTTP, so a service running on/near the data host can sit
inside the network boundary while callers outside it never need
filesystem or bulk access. Defaults to T/GHI/DHI/DNI (the "solar" use
case, matching every caller's behavior before the variables/use_case
params existed) -- see weather.variables for the full registry (also
exposed at GET /v1/weather/variables) and point_query.py for why wind is
preprocessed but wasn't queryable through this API until those params
were added.

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

import pandas as pd
from flask import Flask, Response, jsonify, request

from .. import errors
from ..errors import WeatherAPIError, error_body

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=int(os.environ.get("WEATHER_API_CACHE_SIZE", "256")))
def _cached_point_weather(
    provider: str, latitude: float, longitude: float, year: int, variables: tuple[str, ...]
):
    """Return the point weather timeseries, cached per
    (provider, lat, lon, year, variables).

    Returns a DataFrame indexed on ``time`` with one column per resolved
    variable (see ``weather.variables``). Raises whatever
    ``get_point_weather`` raises; failures are not cached, since
    ``lru_cache`` stores return values only.

    ``variables`` is part of the cache key -- a wind query and a solar
    query for the same (provider, lat, lon, year) get separate entries,
    never collide.

    Cached without TTL because a processed archive is immutable once built.
    The underlying call is expensive (archive open plus DISC/DIRINT DNI/DHI
    reconstruction, see ``point_query.py``) and multiple gateways query the
    same key.
    """
    from weather import get_point_weather

    return get_point_weather(
        latitude, longitude, year, provider=provider, variables=variables
    )

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
            return jsonify(
                error=error_body(
                    errors.SERVICE_UNAVAILABLE, "server misconfigured: no API keys set"
                )
            ), 503

        key = request.headers.get("X-API-Key", "")
        if key not in valid_keys:
            logger.warning(
                "Rejected request from %s: bad/missing API key.",
                request.remote_addr,
            )
            return jsonify(
                error=error_body(errors.INVALID_API_KEY, "invalid or missing X-API-Key")
            ), 401

        if not limiter.allow(key):
            return jsonify(
                error=error_body(errors.RATE_LIMIT_EXCEEDED, "rate limit exceeded")
            ), 429

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
        # Liveness only -- no filesystem I/O. Archive availability is a
        # discovery concern, not a health signal; see /v1/weather/providers.
        return jsonify(status="ok")

    @app.get("/v1/weather/providers")
    def weather_providers() -> Any:
        from weather.registry import list_providers

        providers: dict[str, dict[str, Any]] = {}
        for name in list_providers():
            try:
                providers[name] = {"years": _available_years(name)}
            except OSError as exc:
                providers[name] = {
                    "error": error_body(errors.PROVIDER_LISTING_FAILED, str(exc))
                }
        return jsonify(providers=providers)

    @app.get("/v1/weather/variables")
    def weather_variables() -> Any:
        from weather.variables import USE_CASES, VARIABLES

        return jsonify(
            variables={
                name: {"unit": spec.unit, "description": spec.description}
                for name, spec in VARIABLES.items()
            },
            use_cases={name: list(members) for name, members in USE_CASES.items()},
        )

    @app.get("/v1/weather/point")
    def weather_point() -> Any:
        from weather.variables import resolve_variables

        provider = request.args.get("provider", "")
        try:
            latitude = float(request.args["lat"])
            longitude = float(request.args["lon"])
            year = int(request.args["year"])
        except KeyError as exc:
            return jsonify(
                error=error_body(
                    errors.MISSING_PARAMETER,
                    f"Missing required parameter: {exc.args[0]}",
                    {"parameter": exc.args[0]},
                )
            ), 400
        except ValueError:
            return jsonify(
                error=error_body(
                    errors.NON_NUMERIC_PARAMETER, "lat, lon, year must be numeric"
                )
            ), 400

        if not (-90.0 <= latitude <= 90.0):
            return jsonify(
                error=error_body(
                    errors.COORDINATE_OUT_OF_RANGE,
                    f"lat must be between -90 and 90, got {latitude}",
                    {"parameter": "lat", "value": latitude},
                )
            ), 400
        if not (-180.0 <= longitude <= 180.0):
            return jsonify(
                error=error_body(
                    errors.COORDINATE_OUT_OF_RANGE,
                    f"lon must be between -180 and 180, got {longitude}",
                    {"parameter": "lon", "value": longitude},
                )
            ), 400

        if not provider:
            return jsonify(
                error=error_body(
                    errors.MISSING_PARAMETER,
                    "provider is required",
                    {"parameter": "provider"},
                )
            ), 400

        try:
            variables = resolve_variables(
                variables=request.args.get("variables"),
                use_case=request.args.get("use_case"),
            )
        except WeatherAPIError as exc:
            return jsonify(error=error_body(exc.code, str(exc), exc.details)), 400

        try:
            df = _cached_point_weather(provider, latitude, longitude, year, variables)
        except WeatherAPIError as exc:
            return jsonify(error=error_body(exc.code, str(exc), exc.details)), 400
        except FileNotFoundError as exc:
            return jsonify(error=error_body(errors.ARCHIVE_NOT_FOUND, str(exc))), 404
        except (RuntimeError, KeyError) as exc:
            # point_query.py raises these when the archive itself isn't
            # in a servable state for this query -- an unrepaired
            # ERA5-Land boundary month (RuntimeError), a file that
            # predates the lat/lon-retention convention, or a requested
            # variable this archive predates the export of (KeyError).
            # All are real, actionable server-side data problems, not
            # a bad request or a crash -- surface them as such instead
            # of falling through to Flask's generic unhelpful 500.
            return jsonify(
                error=error_body(errors.SERVICE_UNAVAILABLE, str(exc))
            ), 503

        if request.args.get("format", "parquet").lower() == "json":
            # JSON mode: same cached DataFrame, nested as
            # {"index": [...], "variables": {name: [...]}} rather than
            # flat top-level keys -- a flat shape can't decode into a
            # typed struct (e.g. Go) and lets a variable literally named
            # "index" collide with the timestamps. See #13.
            # NaN -> None so this serializes as JSON `null`, not the bare
            # `NaN` token Python's json module emits by default -- that
            # token is invalid JSON and a strict decoder (e.g. Go's
            # encoding/json) rejects the whole response outright. Real,
            # not hypothetical: ERA5-Land's boundary-repaired archive-start
            # hour is exactly this case (see docs/COUNTRY_SCOPED_ARCHIVES.md).
            def _col(name: str) -> list:
                return [None if pd.isna(v) else float(v) for v in out[name]]

            out = df.reset_index()
            # Data is UTC by construction throughout the pipeline (see
            # providers/*/transform.py, common/dni_reconstruction.py), but
            # get_point_weather()'s return contract is deliberately
            # tz-naive (point_query.py's _finalize/_strip_tz) -- format
            # explicitly as UTC rather than relying on any tz on ts itself.
            payload = {
                "index": [ts.strftime("%Y-%m-%dT%H:%M:%SZ") for ts in out["time"]],
                "variables": {name: _col(name) for name in variables},
            }
            return jsonify(**payload)

        buf = io.BytesIO()
        df.reset_index().to_parquet(buf, index=False)
        return Response(buf.getvalue(), mimetype="application/octet-stream")

    return app
