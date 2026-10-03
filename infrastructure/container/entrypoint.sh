#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Weather Pipeline — Container Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Routes to the appropriate pipeline script based on the PIPELINE_MODE
# environment variable.  All pipeline parameters are also passed via env vars
# so docker-compose.yml stays readable and no shell quoting is needed.
#
# PIPELINE_MODE values
# ─────────────────────
#   single-year   Run test_cosmo_one_year.py for COSMO_YEAR
#   multi-year    Run test_cosmo_multi_year.py for COSMO_FROM_YEAR … COSMO_TO_YEAR
#   merge         Merge monthly NCs → annual NC for COSMO_YEAR (post-processing)
#   percentile    Run test_percentile.py (needs annual NCs from multi-year)
#   check         Validate imports + run unit tests — no data required
#   serve         Run weather serve's point-query HTTP API under gunicorn,
#                 listening on :8080 -- see src/weather/api/README.md
#   (unset)       Forward raw command args to `python -m weather`
#
# Environment variables
# ──────────────────────
#   PIPELINE_MODE           One of the values above (default: unset → passthrough)
#   COSMO_YEAR              Year for single-year / merge         (default: 2018)
#   COSMO_FROM_YEAR         Start year for multi-year            (default: 1995)
#   COSMO_TO_YEAR           End year for multi-year              (default: 2018)
#   COSMO_NCORES            Total worker cores                   (default: 4)
#   COSMO_PARALLEL_YEARS    Concurrent years in multi-year mode  (default: 1)
#   COSMO_WORK_DIR          Data root inside the container       (default: /data)
#   COSMO_PERCENTILES       Comma-separated percentile values    (default: 10,50,90)
#   COSMO_RESUME            Set to 1 to add --resume flag
#   COSMO_SKIP_DOWNLOAD     Set to 1 to add --skip-download flag
#   COSMO_SKIP_DECOMPRESS   Set to 1 to add --skip-decompress flag
#   COSMO_SKIP_DNI          Set to 1 to add --skip-dni flag
#   COSMO_CLEANUP           Delete intermediates after export (default: false,
#                           keep everything). Read directly by the Python
#                           scripts' own EnvSettings.cosmo_cleanup() default --
#                           NOT translated into a CLI flag here (unlike the
#                           other COSMO_* vars above), so there is exactly one
#                           place this behaves differently than a plain
#                           passed-through env var: nowhere. Just set it.
#   WEATHER_API_WORKERS     serve mode: gunicorn worker count    (default: 2)
#   WEATHER_DATA_DIR        serve mode: data root inside the container
#                           (unset here -- weather.settings' own default;
#                           the compose file sets this to /data)
#   WEATHER_API_KEYS        serve mode: required, no default -- see
#                           src/weather/api/README.md
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPTS=/app/src/weather/tests

# ── Shared flag builder ────────────────────────────────────────────────────
_common_flags() {
    [[ "${COSMO_WORK_DIR:-}"          ]] && echo --work-dir "${COSMO_WORK_DIR}"
    [[ "${COSMO_RESUME:-0}"        == "1" ]] && echo --resume
    [[ "${COSMO_SKIP_DOWNLOAD:-0}" == "1" ]] && echo --skip-download
    [[ "${COSMO_SKIP_DECOMPRESS:-0}" == "1" ]] && echo --skip-decompress
    [[ "${COSMO_SKIP_DNI:-0}"      == "1" ]] && echo --skip-dni
}

# ── Route by PIPELINE_MODE ─────────────────────────────────────────────────
case "${PIPELINE_MODE:-}" in

  # ── Single-year COSMO-REA6 pipeline ────────────────────────────────────
  single-year)
    exec python "${SCRIPTS}/test_cosmo_one_year.py" \
        --year   "${COSMO_YEAR:-2018}" \
        --ncores "${COSMO_NCORES:-4}" \
        $(_common_flags)
    ;;

  # ── Multi-year COSMO-REA6 pipeline ─────────────────────────────────────
  multi-year)
    exec python "${SCRIPTS}/test_cosmo_multi_year.py" \
        --from-year      "${COSMO_FROM_YEAR:-1995}" \
        --to-year        "${COSMO_TO_YEAR:-2018}" \
        --ncores         "${COSMO_NCORES:-4}" \
        --parallel-years "${COSMO_PARALLEL_YEARS:-1}" \
        $(_common_flags)
    ;;

  # ── Post-processing: merge monthly NCs → annual NC ─────────────────────
  merge)
    year="${COSMO_YEAR:-2018}"
    out="${COSMO_WORK_DIR:-/data}/output"
    exec python -m weather.common.merge \
        --input  "${out}/COSMO_REA6_${year}_??_all_attrs.nc" \
        --output "${out}/COSMO_REA6_${year}_annual_all_attrs.nc"
    ;;

  # ── Percentile representative-year files ───────────────────────────────
  percentile)
    args=(
      python "${SCRIPTS}/test_percentile.py"
      --from-year "${COSMO_FROM_YEAR:-1995}"
      --to-year   "${COSMO_TO_YEAR:-2018}"
      --ncores    "${COSMO_NCORES:-4}"
    )
    [[ "${COSMO_WORK_DIR:-}"      ]] && args+=(--work-dir   "${COSMO_WORK_DIR}")
    [[ "${COSMO_PERCENTILES:-}"   ]] && args+=(--percentiles "${COSMO_PERCENTILES}")
    exec "${args[@]}"
    ;;

  # ── weather serve: point-query HTTP API ────────────────────────────────
  serve)
    exec gunicorn --bind 0.0.0.0:8080 --workers "${WEATHER_API_WORKERS:-2}" \
        "weather.api.app:create_app()"
    ;;

  # ── Image smoke test — no data required ────────────────────────────────
  check)
    echo "=== Import check ==="
    python - <<'PYEOF'
import sys
packages = [
    "xarray", "cfgrib", "dask", "h5py", "netCDF4",
    "numpy", "scipy", "pvlib", "requests",
]
failed = []
for pkg in packages:
    try:
        __import__(pkg)
    except ImportError as e:
        failed.append(f"  {pkg}: {e}")

if failed:
    print("FAIL — missing packages:", file=sys.stderr)
    print("\n".join(failed), file=sys.stderr)
    sys.exit(1)

print(f"OK: {len(packages)} packages importable")
PYEOF
    echo ""
    echo "=== weather package ==="
    python -m weather info
    echo ""
    echo "=== unit tests (no data required) ==="
    python -m pytest /app/src/weather/tests/test_validation.py -v --tb=short
    ;;

  # ── Passthrough: any other PIPELINE_MODE or no mode set ────────────────
  *)
    if [[ $# -gt 0 ]]; then
      exec python -m weather "$@"
    else
      exec python -m weather info
    fi
    ;;

esac
