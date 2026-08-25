#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# weather serve launcher
# ──────────────────────────────────────────────────────────────────────────
# Starts the weather point-query HTTP API (src/weather/api/) detached, for
# downstream consumers (e.g. buem) that can't reach the processed archives'
# filesystem directly -- see src/weather/api/README.md for the API itself.
#
# WEATHER_API_KEYS is READ from .env in the repo root, never hardcoded here
# -- this script is committed to the repo, the key is not. Add it to .env
# (gitignored) before running this, e.g.:
#   WEATHER_API_KEYS=<random-token>
#
# Usage:
#   bash scripts/launch_weather_serve.sh [--host HOST] [--port PORT]
#
# Defaults: --host 0.0.0.0 --port 8091 (0.0.0.0 so the service is reachable
# from outside this machine -- e.g. via an SSH tunnel, since most deployments
# of this repo sit behind a firewall/VPN with no direct inbound access).
#
# Stop it later with:  kill "$(cat logs/weather_serve.pid)"
# (or the check/stop/restart commands documented in
# src/weather/api/README.md, which wrap this same PID file).
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${WEATHER_SERVE_HOST:-0.0.0.0}"
PORT="${WEATHER_SERVE_PORT:-8091}"
# Standardized, durable locations -- NOT /tmp (which can be cleaned on
# reboot/periodic tmpfiles cleanup, unlike the repo checkout itself).
# logs/ is gitignored (see .gitignore), so neither file gets committed.
LOG_FILE="${WEATHER_SERVE_LOG:-${REPO_DIR}/logs/weather_serve.log}"
PID_FILE="${REPO_DIR}/logs/weather_serve.pid"
mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    echo "[launch_weather_serve] Already running (pid $(cat "${PID_FILE}"))." >&2
    echo "  Stop it first: kill \$(cat ${PID_FILE})" >&2
    exit 1
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Load .env (WEATHER_API_KEYS lives here, not in this script) ────────────
# Stripped through `tr -d '\r'` so a CRLF-saved .env (e.g. edited on
# Windows, then copied to the server) doesn't break `source`.
if [ -f "${REPO_DIR}/.env" ]; then
    echo "[launch_weather_serve] Loading .env from ${REPO_DIR}/.env"
    set -a
    source <(tr -d '\r' < "${REPO_DIR}/.env")
    set +a
fi

if [ -z "${WEATHER_API_KEYS:-}" ]; then
    echo "[launch_weather_serve] ERROR: WEATHER_API_KEYS is not set." >&2
    echo "  Add it to ${REPO_DIR}/.env, e.g.:" >&2
    KEYGEN="python -c 'import secrets; print(secrets.token_urlsafe(32))'"
    echo "  WEATHER_API_KEYS=\$(${KEYGEN})" >&2
    exit 1
fi

# ── Conda environment (PATH-only activation, no conda binary required) ─────
CONDA_ENV_DIR="${WEATHER_CONDA_ENV_DIR:-${HOME}/.conda/envs/weather_env}"
if [ ! -d "${CONDA_ENV_DIR}/bin" ]; then
    echo "[launch_weather_serve] ERROR: conda env not found: ${CONDA_ENV_DIR}" >&2
    exit 1
fi
export PATH="${CONDA_ENV_DIR}/bin:${PATH}"

cd "${REPO_DIR}"
echo "[launch_weather_serve] Starting weather serve on ${HOST}:${PORT} (log: ${LOG_FILE})"
nohup python -m weather serve --host "${HOST}" --port "${PORT}" \
    > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"
disown
echo "[launch_weather_serve] launched pid $(cat "${PID_FILE}") (pid file: ${PID_FILE})"
