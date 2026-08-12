"""Flask app factory for weather serve.

See the package docstring (__init__.py) for what this API is and why it
exists, and README.md in this directory for deployment notes.
"""

from __future__ import annotations

import logging
import os

from flask import Flask, Response, request

from .auth import make_authenticate
from .rate_limit import RateLimiter, make_rate_limit_headers
from .views.health import HealthView
from .views.point import PointView
from .views.providers import ProvidersView
from .views.validate import ValidateView
from .views.variables import VariablesView

logger = logging.getLogger(__name__)


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


def create_app() -> Flask:
    app = Flask(__name__)
    limiter = RateLimiter(
        max_requests=int(os.environ.get("WEATHER_API_RATE_LIMIT", "60")),
        window_seconds=60.0,
    )

    app.before_request(make_authenticate(limiter))
    app.after_request(_audit_log)
    app.after_request(make_rate_limit_headers(limiter))

    # /v1/weather/health, not /v1/health -- other services behind the same
    # Orchestrator expose their own /health too; nesting under /weather/
    # avoids a path collision if those are ever aggregated behind one host.
    app.add_url_rule("/v1/weather/health", view_func=HealthView.as_view("health"))
    app.add_url_rule(
        "/v1/weather/providers", view_func=ProvidersView.as_view("weather_providers")
    )
    app.add_url_rule(
        "/v1/weather/variables", view_func=VariablesView.as_view("weather_variables")
    )
    app.add_url_rule("/v1/weather/point", view_func=PointView.as_view("weather_point"))
    app.add_url_rule(
        "/v1/weather/validate", view_func=ValidateView.as_view("weather_validate")
    )

    return app
