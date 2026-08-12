"""API-key authentication for weather serve."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from flask import jsonify, request

from .. import errors
from ..errors import error_body
from .rate_limit import RateLimiter

logger = logging.getLogger(__name__)


def _valid_api_keys() -> set[str]:
    """Static API keys from WEATHER_API_KEYS (comma-separated).

    A minimum viable control, not a substitute for network-level
    restrictions (firewall / IP allowlist) -- see README.md.
    """
    raw = os.environ.get("WEATHER_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def make_authenticate(limiter: RateLimiter) -> Callable[[], Any]:
    """Build the before_request hook: API-key check, then rate limit."""

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

    return _authenticate
