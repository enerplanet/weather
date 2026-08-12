"""GET /v1/weather/point -- hourly weather for one location/year."""

from __future__ import annotations

import functools
import io
import os
from collections.abc import Mapping
from typing import Any

import pandas as pd
from flask import Response, jsonify, request
from flask.views import MethodView

from ... import errors
from ...errors import WeatherAPIError, error_body


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

    Must stay a module-level function, not a ``PointView`` method: an
    ``lru_cache``-decorated instance method would key on ``self``, and
    Flask constructs a new view instance per request by default
    (``MethodView.init_every_request``), so every call would miss --
    silently disabling the cache.
    """
    from weather import get_point_weather

    return get_point_weather(
        latitude, longitude, year, provider=provider, variables=variables
    )


def parse_point_query(
    args: Mapping[str, str],
) -> tuple[float, float, int, str, tuple[str, ...]]:
    """Parse and validate provider/lat/lon/year/variables/use_case from
    query args. Raises ``WeatherAPIError`` with the right code on any
    problem. Shared by ``/v1/weather/point`` and ``/v1/weather/validate``.

    Returns ``(latitude, longitude, year, canonical_provider, variables)``.
    """
    from weather.point_query import resolve_provider
    from weather.variables import resolve_variables

    try:
        latitude = float(args["lat"])
        longitude = float(args["lon"])
        year = int(args["year"])
    except KeyError as exc:
        raise WeatherAPIError(
            errors.MISSING_PARAMETER,
            f"Missing required parameter: {exc.args[0]}",
            details={"parameter": exc.args[0]},
        ) from exc
    except ValueError as exc:
        raise WeatherAPIError(
            errors.NON_NUMERIC_PARAMETER, "lat, lon, year must be numeric"
        ) from exc

    if not (-90.0 <= latitude <= 90.0):
        raise WeatherAPIError(
            errors.COORDINATE_OUT_OF_RANGE,
            f"lat must be between -90 and 90, got {latitude}",
            details={"parameter": "lat", "value": latitude},
        )
    if not (-180.0 <= longitude <= 180.0):
        raise WeatherAPIError(
            errors.COORDINATE_OUT_OF_RANGE,
            f"lon must be between -180 and 180, got {longitude}",
            details={"parameter": "lon", "value": longitude},
        )

    provider = args.get("provider", "")
    if not provider:
        raise WeatherAPIError(
            errors.MISSING_PARAMETER, "provider is required",
            details={"parameter": "provider"},
        )
    canonical_provider = resolve_provider(provider)

    variables = resolve_variables(
        variables=args.get("variables"), use_case=args.get("use_case"),
    )
    return latitude, longitude, year, canonical_provider, variables


class PointView(MethodView):
    def get(self) -> Any:
        try:
            latitude, longitude, year, provider, variables = parse_point_query(
                request.args
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
