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

Package layout: app.py (Flask app factory), auth.py/rate_limit.py
(cross-cutting request hooks), views/ (one flask.views.MethodView
subclass per route).
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
