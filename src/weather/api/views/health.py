"""GET /v1/weather/health -- liveness check."""

from __future__ import annotations

from typing import Any

from flask import jsonify
from flask.views import MethodView


class HealthView(MethodView):
    def get(self) -> Any:
        # Liveness only -- no filesystem I/O. Archive availability is a
        # discovery concern, not a health signal; see /v1/weather/providers.
        return jsonify(status="ok")
