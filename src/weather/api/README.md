# Point-query HTTP API

**Status: deployed via Docker (`infrastructure/container/docker-compose.serve.yml`,
`weather` namespace). Not wired into this repo's own CI/packaging defaults.**

## Why this exists

buem's production deployment cannot reach this repo's processed archives
directly: they live on a university server behind VPN, and a
request-serving container can't join a human VPN client the way a developer
does with `ssh sd26`. This exposes exactly one operation over HTTP so a
service running on/near the data host can sit inside that network boundary,
while callers outside it never need filesystem or bulk access.

## Scope

`GET /v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018`
→ `weather.get_point_weather(latitude, longitude, year, provider=provider)`,
returned as a parquet-encoded body (`application/octet-stream`; the reset
index column is first, then one column per resolved variable, in order).

Add `&format=json` to get the same data as a JSON body instead
(`{"index": [...], "T": [...], ...}`, one key per resolved variable,
shaped to match buem's `WeatherConfig` dict directly for the default
`solar` use_case) — for a caller that doesn't want a parquet-parsing
dependency just to consume this endpoint. `NaN` values serialize as JSON
`null`.

By default this returns temperature/irradiance (`T`/`GHI`/`DHI`/`DNI`,
the `solar` use_case). Add `&use_case=wind` for wind
(`WS_10M`/`U_10M`/`V_10M`), or `&variables=T,WS_10M` to name exact
variables directly (at most one of `variables`/`use_case`; see
`weather.variables` for the full registry). `GET /v1/weather/variables`
lists every variable's name/unit/description and every `use_case`'s
members, so a caller doesn't need to already know the meteorological
variable names.

`GET /v1/health` → per-provider list of years with a processed archive,
derived from filenames already on disk. Deliberately does not expose a raw
directory listing.

Deliberately **not** exposed: file listing, bulk/archive download, anything
beyond this single point query already at the heart of `weather`'s own
public API. Keeping the network surface this narrow is the point — a
security review of "one typed query operation" is a much smaller ask than
"open network access to the data host".

## Auth (minimum viable, not sufficient on its own)

Static API keys via `WEATHER_API_KEYS` (comma-separated), checked against
the `X-API-Key` header. A per-key in-memory rate limiter
(`WEATHER_API_RATE_LIMIT`, default 60 req/min) guards against the "many
small point queries reconstruct the bulk archive" risk. Every request is
audit-logged (key prefix, path, status, remote address).

This is not a substitute for network-level restrictions — real deployment
should still pair this with a firewall/IP allowlist scoped to buem's known
egress, per the production-access design discussion (see buem's CLAUDE.md,
"weather-archive-access"). The rate limiter is in-memory and per-process —
fine for the single-process dev server below, not for a multi-worker WSGI
deployment (would need a shared store, e.g. redis, at that point).

## Running it

Local dev server:

```bash
pip install -e ".[api,pointquery,solar,parquet]"
export WEATHER_API_KEYS="dev-key-change-me"
weather serve --host 127.0.0.1 --port 8080
```

`weather serve` runs Flask's dev server — fine for local testing, **not**
for production (no concurrency, no TLS).

Docker (gunicorn, matches buem's own `infrastructure/container/` shape for
its API):

```bash
export WEATHER_API_KEYS="dev-key-change-me"   # in .env, or shell export
docker compose -f infrastructure/container/docker-compose.serve.yml \
    up -d --build
```

Runs in its own `weather` Compose namespace, deliberately not joined to
any one consumer's namespace (e.g. `building-simulation`) — this service
has more than one downstream consumer in mind (BuEM today, PV/wind
named as future ones). A consumer in a different Compose project reaches
it via its published host port (`WEATHER_API_PORT`, default 8090) — see
the compose file's own header comment for the `host.docker.internal`
pattern.

## Consumer integration

Per `decisions/2026-08-07-buem-weather-access-architecture.md` (private
vault, not in this repo): **no service calls this API directly.** The
only sanctioned caller is a future Orchestrator, which resolves weather
and hands it to BuEM (and other model services) as part of the request
it already builds — BuEM's own `/api/process` accepts a pre-resolved
`buem.weather` block for exactly this (see `enerplanet/buem#10`). Neither
`buem-gateway` nor BuEM itself fetches from this API — confirmed
directly by the Orchestrator's own developer, not inferred.

## Still open before this can be real (not decided here)

- Where this actually runs relative to the data host (on `sd26` itself vs.
  a small gateway host) and how the Orchestrator's production egress
  reaches it — needs the IT conversation flagged in buem's CLAUDE.md, not
  a code change.
- The Orchestrator itself doesn't exist in code yet, so nothing calls this
  API in production today — this is architectural placement, not a
  working integration.
