"""GET /v1/weather/health -- liveness check."""

from __future__ import annotations

from typing import Any

from flask import jsonify
from flask.views import MethodView

#: Route path. Exempt from API-key auth and rate limiting (see
#: auth.make_authenticate and rate_limit.make_rate_limit_headers) so
#: container/orchestrator health checks and uptime monitors can reach it
#: without a credential. The handler returns a constant and does no I/O.
HEALTH_PATH = "/v1/weather/health"


class HealthView(MethodView):
    def get(self) -> Any:
        # Liveness only -- no filesystem I/O. Archive availability is a
        # discovery concern, not a health signal; see /v1/weather/providers.
        return jsonify(status="ok")
