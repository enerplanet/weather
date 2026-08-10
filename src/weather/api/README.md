# Point-query HTTP API (scaffold)

**Status: scaffold, 2026-08-04. Not deployed, not wired into this repo's CI,
not committed/pushed without review.**

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
index column is first, then `T`/`GHI`/`DHI`/`DNI`).

Add `&format=json` to get the same data as a JSON body instead
(`{"index": [...], "T": [...], "GHI": [...], "DHI": [...], "DNI": [...]}`,
shaped to match buem's `WeatherConfig` dict directly) — for a caller that
doesn't want a parquet-parsing dependency just to consume this endpoint.
`NaN` values serialize as JSON `null`.

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

```bash
pip install -e ".[api,pointquery,solar,parquet]"
export WEATHER_API_KEYS="dev-key-change-me"
weather serve --host 127.0.0.1 --port 8080
```

`weather serve` runs Flask's dev server — fine for local testing, **not**
for production (no concurrency, no TLS). A real deployment should run this
app under gunicorn/similar, same as buem's own `infrastructure/container/`
does for its API.

## buem-side integration

`buem`'s `weather_cache.py::get_or_fetch_weather()` has a matching remote
branch, gated by `WEATHER_API_URL`/`WEATHER_API_KEY` — see that repo's own
scaffold changes. Unset (the default), buem's behavior is entirely
unchanged (local `data_dir`/archive path, exactly as today).

## Still open before this can be real (not decided here)

- Where this actually runs relative to the data host (on `sd26` itself vs.
  a small gateway host) and how buem's production egress reaches it —
  needs the IT conversation flagged in buem's CLAUDE.md, not a code change.
- `cosmo-rea6`/`era5-land` health/point-query support is already wired
  (same `get_point_weather` call, provider-agnostic) but untested against
  real archives for those two providers as of this scaffold.
