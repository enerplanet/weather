"""GET /v1/weather/validate -- pre-flight request validation, no archive access."""

from __future__ import annotations

from typing import Any

from flask import jsonify, request
from flask.views import MethodView

from ...errors import WeatherAPIError, error_body
from .point import parse_point_query


class ValidateView(MethodView):
    def get(self) -> Any:
        # Structural validation only -- parameters, ranges, and
        # provider/variable/use_case names. Deliberately does not touch
        # the archive: /v1/weather/point's own 404 already fails before
        # opening any file (a cheap glob/exists check precedes every real
        # extraction), so there is no "wasted time" case here to guard
        # against by duplicating that check.
        try:
            _latitude, _longitude, period, provider, variables = parse_point_query(
                request.args
            )
        except WeatherAPIError as exc:
            return jsonify(error=error_body(exc.code, str(exc), exc.details)), 400

        period_key = "year" if isinstance(period, int) else "scenario"
        return jsonify(
            valid=True,
            resolved={
                "provider": provider,
                period_key: period,
                "variables": list(variables),
            },
        )
