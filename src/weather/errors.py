"""Structured error codes shared by weather's library raise sites and
weather.api's HTTP handler -- one place naming every distinct failure
mode so a caller (HTTP or Python) can branch on a stable ``code`` instead
of matching message text. See #13.
"""

from __future__ import annotations

from typing import Any

# 400 -- bad request
MISSING_PARAMETER = "missing_parameter"
NON_NUMERIC_PARAMETER = "non_numeric_parameter"
COORDINATE_OUT_OF_RANGE = "coordinate_out_of_range"
UNKNOWN_PROVIDER = "unknown_provider"
UNKNOWN_VARIABLE = "unknown_variable"
UNKNOWN_USE_CASE = "unknown_use_case"
VARIABLES_USE_CASE_CONFLICT = "variables_use_case_conflict"
VARIABLES_USE_CASE_REQUIRED = "variables_use_case_required"

# 401 / 404 / 429 / 503
INVALID_API_KEY = "invalid_api_key"
ARCHIVE_NOT_FOUND = "archive_not_found"
RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
SERVICE_UNAVAILABLE = "service_unavailable"

# Per-provider failure embedded in GET /v1/weather/providers' 200 body
PROVIDER_LISTING_FAILED = "provider_listing_failed"


class WeatherAPIError(ValueError):
    """A library-raised error carrying a stable, machine-readable code.

    Subclasses ``ValueError`` (not ``Exception``): ``resolve_variables``/
    ``get_point_weather`` have always raised ``ValueError`` for bad
    input, and existing non-HTTP callers doing ``except ValueError``
    must keep working unchanged.
    """

    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, Any] = details or {}


def error_body(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the ``error`` object every HTTP error response carries:
    ``{"code": ..., "message": ..., "details": {...}}``.

    ``details`` is omitted entirely when empty, never emitted as ``{}``.
    """
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return body
