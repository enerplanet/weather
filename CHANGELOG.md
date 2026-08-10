# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.2.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Docs

- `docs/openapi.yaml`: `use_case`/`variables` now render as selectable
  options (enum), same as `provider` already did, instead of free-text
  fields. Added a concrete "discover, then query" workflow example.
  New test (`test_openapi_sync.py`) keeps these enums from silently
  drifting from the `weather.variables` registry -- no build step
  connects the two otherwise.

### Changed

- **Breaking**: `GET /v1/health` no longer returns `providers` -- it's
  liveness only now (`{"status": "ok"}`, no filesystem I/O). Moved to
  new `GET /v1/weather/providers`. See #7.

### Added

- `infrastructure/container/docker-compose.serve.yml` -- runs `weather
  serve` under gunicorn in Docker (`PIPELINE_MODE=serve`, new
  `entrypoint.sh` case, reuses the existing COSMO-pipeline image). Its
  own `weather` Compose namespace, deliberately not joined to any one
  consumer's namespace (e.g. `building-simulation`), since this service
  has more than one downstream consumer in mind. See #6.
- `weather.variables` -- canonical registry of every variable
  `get_point_weather()`/`weather serve` can return (name, unit,
  description) and the named `use_case` shortcuts that group them
  (`solar`: T/GHI/DHI/DNI, `wind`: WS_10M/U_10M/V_10M). `get_point_weather()`
  and `GET /v1/weather/point` both gain `variables`/`use_case` params
  (at most one). New `GET /v1/weather/variables` discovery endpoint.
  See #5.

### Changed

- `point_query.py`'s per-provider extraction (`_get_point_regular_grid`,
  `_get_point_cosmo_rea6`) now conditionally pulls only the requested
  variables -- e.g. a wind-only query skips the pvlib DNI/DHI
  reconstruction entirely. Defaults to the `solar` use_case
  (T/GHI/DHI/DNI) when neither `variables` nor `use_case` is given,
  matching every existing caller's behavior before these params existed.
- `weather serve`'s in-process response cache key now includes the
  resolved variables tuple, not just (provider, lat, lon, year) -- a
  wind query and a solar query for the same location/year no longer
  collide in the cache.

## [1.8.0] - 2026-08-06

### Fixed

- `validate.py`'s "Package Structure" check looked for `docker-compose.yml`
  at the repo root; it lives at `infrastructure/container/docker-compose.yml`
  (matching the `Dockerfile` entry in the same check list). Found while
  running this repo's own release-workflow CI mirror.
- `percentile_index.py` (all three providers) — `ref_coords` were captured
  as bare coordinate values when materializing a reference dataset's
  coords before building each month's mosaic. COSMO-REA6's 2-D
  `latitude`/`longitude` (indexed by `(y, x)`, unlike ERA5-Land/MERRA-2's
  1-D dimension coordinates) made `xr.Dataset(coords={...})` raise
  `cannot set variable ... without explicit dimension names`. Fixed by
  capturing `(dims, values)` uniformly for every coordinate — correct for
  both the 2-D COSMO case and the 1-D ERA5-Land/MERRA-2 case (a no-op
  there, applied for consistency). Verified against a full COSMO-REA6
  percentile run against the real 1995-2019 archive (36 output files,
  spot-checked across seasons/percentiles/both year-pool sizes) and the
  existing MERRA-2 percentile output (1980-2025).

### Added

- `weather serve` — thin, opt-in HTTP point-query API (`src/weather/api/`,
  new `api` extra requiring `flask>=3.0`). Exposes `GET /v1/weather/point`
  (→ `get_point_weather()` as parquet) and `GET /v1/health` (per-provider
  processed-year listing), with static API-key auth (`WEATHER_API_KEYS`)
  and a per-key rate limiter. Verified against the real merra-2/cosmo-rea6
  archives (health check, point queries incl. a partial year, 404/401
  cases); a real gap found and fixed during that verification:
  `point_query.py`'s `RuntimeError` (e.g. an unrepaired ERA5-Land boundary
  month) fell through `app.py`'s exception handling to a generic 500 — now
  returns a clear 503 with the underlying message. Scaffold status — not
  wired into this repo's own CI, not deployed; see
  `src/weather/api/README.md`.
- `tests/inspect_cosmo_nc.py` — single-file COSMO-REA6 diagnostic
  (attribute completeness, value profile, spot check), mirroring the
  existing ERA5-Land `diagnose_nc.py`.
- `tests/inspect_merra2_percentile.py` — spot-checks a MERRA-2 percentile
  output file (attribute completeness, `source_year` diversity, GHI/T
  value sanity).

### Changed

- `infrastructure/env/weather_env.yml`'s dependency list alphabetized;
  also merged a duplicate `bottleneck` entry and dropped the
  now-inapplicable "Optional but recommended" sub-heading.

## [1.7.0] - 2026-08-04

### Fixed

- **`get_point_weather()` — 3 real bugs found and fixed against real archives**
  (previously only smoke-tested against synthetic fixtures):
  - MERRA-2: a mid-migration archive (some months regenerated with the
    canonical `T`/`U_10M`/etc. names, others still on legacy `T2M`/`U10M`/
    etc.) merged into two separate variables per name, and the old
    `"T" in cell` check silently picked the mostly-NaN one. `point_query.py`
    now renames each file's legacy name to canonical per file, before
    combining.
  - COSMO-REA6: a mid-migration archive (some months with 2-D lat/lon
    coordinates, others without) raised `ValueError: 'latitude' not present
    in all datasets'` from `combine="by_coords"`. Fixed by resolving the
    nearest cell index from whichever month has coordinates and reusing it
    across every month.
  - ERA5-Land: an unrepaired month's first-hour `GHI` (a raw accumulated
    daily total, not an hourly flux — see `boundary_repair.py` below) was
    silently returned as-is. `point_query.py` now checks `boundary_status`
    per file and raises `RuntimeError` instead.
- **`export_netcdf()` — real data-corruption bug, all three providers**
  (`cosmo_rea6`/`era5_land`/`merra2`). Writing directly to the final path
  with no temp-file-then-rename meant an interrupted write (Ctrl-C, OOM,
  crash) left a truncated file at the final name, which `--resume`'s naive
  `Path.exists()` check then treated as complete forever. All three
  `export.py` now write to a sibling `.tmp` path and `Path.replace()` only
  on success.
- Packaging: dropped a real, undeclared runtime dependency on `dask` from
  the ERA5-Land/MERRA-2 point-query path (`xr.open_mfdataset` requires the
  dask chunk manager even with no `chunks=` argument), contradicting the
  `pointquery` extra's explicit no-dask design goal.

### Added

- `COSMO_MAX_RETRIES` / `ERA5_CDS_MAX_RETRIES` / `MERRA2_OPENDAP_MAX_RETRIES`
  — download retry-attempt count is now a real config knob for all three
  providers (was: COSMO hardcoded to 10, ERA5-Land configurable but
  defaulting to 5, MERRA-2 hardcoded to 5 with no knob at all). All three
  now default to 10, resolved via `EnvSettings`.
- `providers/era5_land/boundary_repair.py`'s `repair_boundaries()` now runs
  automatically as `run_pipeline()`'s STEP 3/3 after every transform+export
  (new `repair: bool = True` parameter), for just the months that run
  touched — no separate manual step needed for a run through this repo.
- `tests/test_era5_boundary_repair.py` (7 tests against synthetic fixtures).

### Changed

- ERA5-Land's boundary-repair logic relocated from `tests/
  repair_month_boundaries.py` to `providers/era5_land/boundary_repair.py`
  — real pipeline logic that mutates output files belongs in the provider
  package, not `tests/` (mirrors `percentile_index.py`'s existing
  precedent). Breaking CLI path change for anyone invoking the old script
  path directly (same category of change as the COSMO `--cleanup` flag
  rename in 1.5.3).
- `CLAUDE.md`/`.claude/` are no longer tracked in git (internal AI-agent
  working notes, not meant for public browsing) — see `.gitignore`.

## [1.6.0] - 2026-07-30

### Added

- **Cross-provider attribute naming unification.** COSMO-REA6, ERA5-Land, and
  MERRA-2 now share one canonical output name per physical quantity wherever
  the quantity is genuinely the same across providers (formulas still differ
  by design where the physics differs — see `docs/provider_differences.md`'s
  new "Attribute naming reference" table for the full raw-source -> canonical
  mapping):
  - `T_DEW` (dew point): COSMO derives it (`dewpoint_from_rh()`, algebraic
    inverse of the existing Magnus RH formula — DWD's real `hourly/2D/`
    directory listing confirmed no native dew-point field exists and no
    better free derivation is available); ERA5-Land's native `d2m` renamed;
    MERRA-2 gained a new raw attribute `T2MDEW` (free — same `slv` collection
    already fetched) renamed to match.
  - `ALBEDO`: COSMO gained a new raw attribute `SOBS_RAD` (net shortwave,
    instantaneous) and derives `ALBEDO = (GHI - SOBS_RAD) / GHI`, NaN at
    night; ERA5-Land's native `fal` (forecast/total albedo) renamed — `asn`
    (snow-only albedo, narrower, no cross-provider equivalent) intentionally
    left unrenamed; MERRA-2's native `ALBEDO` unchanged.
  - `SNOWFALL`: COSMO's `SNOW_CON`+`SNOW_GSP` combined into one derived
    field; ERA5-Land's `sf` and MERRA-2's `PRECSNOLAND` renamed.
  - `SNOW_DEPTH`, `PS`, `U_10M`/`V_10M`: renamed from each provider's raw
    name (`H_SNOW`/`sde`(see Fixed)/`SNODP`; `sp`; `u10`/`v10`,
    `U10M`/`V10M`) to the shared canonical names.
- COSMO's `build_month_dataset` now also keeps raw `U_10M`/`V_10M` alongside
  the derived `WS_10M` scalar, matching ERA5-Land/MERRA-2 (previously COSMO
  was the only one of the three that discarded the raw components).
- `docs/provider_differences.md`: new "Attribute naming reference" section
  (raw source -> canonical output per provider, plus a table of
  provider-unique fields with no cross-provider equivalent: `asn`, `QV2M`,
  `U_2M`/`V_2M`, `U_50M`/`V_50M`).

### Fixed

- **MERRA-2 GES DISC stream-number 404 on reprocessed months (2020/2021).**
  `downloader.py` hardcoded a single stream number (400) for the entire
  2011-present range, but NASA reprocessed September 2020 and June-September
  2021 under runid 401 instead — a scattered, non-contiguous set of months,
  not a clean date range. `_fetch` now falls back from the primary stream to
  `primary + 1` on a 404 (via a new `_StreamNotFound`, not `OSError`, so it
  doesn't burn 5 useless backoff retries on what's a permanent failure
  first). Verified live: the full 1980-2025 archive (46 years, 552 monthly
  files) now completes and passes `verify_merra2_months.py`'s continuity
  check across every year boundary, including 2019->2020->2021->2022.
- **ERA5-Land's `snow_depth` was mapped to the wrong CDS variable.**
  `downloaded_attributes.py`'s entry used `e5l_name: "sd"` and described the
  field as water-equivalent depth — genuinely different from COSMO's
  `H_SNOW`/MERRA-2's `SNODP` (physical depth), so it was deliberately left
  unrenamed. That was wrong: the actual CDS request variable is `snow_depth`
  (confirmed against the real request payload), which decodes via cfgrib to
  GRIB shortName `sde` — physical depth in meters, the same quantity as the
  other two all along. `sd` is CDS's *other* variable
  (`snow_depth_water_equivalent`), never requested. Verified three ways: the
  real CDS request payload, the raw GRIB's own decoded metadata
  (`GRIB_shortName 'sde'`), and the already-processed 2018-03 output (the
  variable was literally named `sde`). The mislabeling never affected any
  actual downloaded/exported value — the water-equivalent conversion the old
  entry described was never implemented either, so this is a naming/
  documentation fix, not a data correction. Now renamed to canonical
  `SNOW_DEPTH` alongside the other two. `compare_providers.py`'s lookup and
  `docs/provider_differences.md`'s snow-depth section corrected to match.
- `weather_env`'s `mkl` BLAS build collided with `cupy`'s bundled CUDA BLAS DLLs
  (cublas/nvblas) on Windows, crashing numpy>=2's `blas_fpe_check()` self-test on
  plain `import numpy` (Windows fatal exception `0xc06d007f`) — broke the env
  entirely, not just a version-compat issue. Fixed by pinning
  `libblas=*=*openblas` in `infrastructure/env/weather_env.yml`.

### Changed

- MERRA-2's `percentile_index.py` run for real against the full 46-year
  archive (previously only smoke-tested against a single year, where
  `source_year` output is trivially degenerate): all 36 output files
  written, genuine per-cell `source_year` diversity confirmed (P50 draws
  from 45-46 of 46 years per month, P10/P90 from 32-43).
  `verify_merra2_months.py`'s `T2M` plausible-range sanity check widened
  from `(-40, 45)` to `(-55, 55)` — the Europe box spans 34N (Saharan
  margin) to 72N (Arctic Scandinavia), so both ends of the wider range are
  real climate, not corrupted data, once evaluated over 46 years instead of
  one.
- Removed the `numpy<3`/`pandas<3` upper-bound caps from `pyproject.toml`/
  `meta.yaml` (floors only now: `numpy>=1.26`, `pandas>=2.0`) — not actually
  enforced in practice (`weather_env.yml` already left them unbounded) and the
  full `pytest` suite (55 passed / 2 skipped, 1 unrelated eccodes-library gap)
  passes clean on numpy 2.4.4/pandas 3.0.3 + openblas.
- Removed unused `cupy` (and its CUDA dependencies) from
  `infrastructure/env/weather_env.yml` — no GPU work was actually being done
  with it. `libblas=*=*openblas` kept regardless (see Fixed above): already
  verified working, no reason to revert to an untested mkl-without-cupy config.

## [1.5.3] - 2026-07-29

### Added

- **`weather.common.cli_flags`**: `add_cleanup_flag`/`add_resume_flag`/
  `add_skip_download_flag`/`add_skip_decompress_flag` — the one place
  every `test_<provider>_{one_month,one_year,multi_year}.py` script (all
  9) now wires its shared CLI flags from, instead of each script
  hand-declaring its own `argparse` boilerplate. Closes the gap that let
  COSMO-REA6 drift onto a different flag (`--no-cleanup`, negative
  polarity) than ERA5-Land/MERRA-2 (`--cleanup`, positive) for the same
  underlying `*_CLEANUP` setting.
- **`tests/test_point_query.py`**: permanent regression coverage (11
  tests) for `point_query.py`/`common/dni_reconstruction.py`/
  `common/geo_lookup.py` — synthetic NetCDFs shaped like each provider's
  real export (ERA5-Land/MERRA-2's `y`/`x` + 1-D aux-coord grid, COSMO's
  `y`/`x` + 2-D coord grid), including a regression check that a
  pre-lat/lon-fix COSMO archive raises `KeyError` rather than silently
  returning wrong data.
- **`weather.get_point_weather(latitude, longitude, year, provider=...)`**
  (new `weather/point_query.py`, re-exported from `weather/__init__.py`):
  a lightweight, downstream-facing entry point that extracts hourly
  `T`/`GHI`/`DHI`/`DNI` for the nearest already-processed grid cell to a
  location — no download/transform pipeline involved. Requires the new
  `pointquery` extra (`xarray`, `netcdf4`) plus `solar` (`pvlib`) for
  DNI/DHI reconstruction; never pulls in `cfgrib`/`dask`/`eccodes`/
  `pyproj`. This is the intended integration point for downstream
  per-building consumers (e.g. `buem`) that previously had no dynamic,
  location-aware way to query this package.
- **`weather.common.geo_lookup.find_nearest_cell(ds, lat, lon)`**: nearest
  grid-cell index lookup against real 2-D `latitude`/`longitude`
  coordinates. Used by `get_point_weather` for COSMO-REA6's rotated-pole
  grid (ERA5-Land/MERRA-2 use `ds.sel(..., method="nearest")` directly,
  since they're on a regular lat/lon grid already).
- **`weather.common.dni_reconstruction.reconstruct_dni_dhi(...)`**: the
  pvlib DIRINT/DISC point-of-use DNI/DHI decomposition, consolidated from
  three previously-duplicated implementations (see Changed).

### Changed

- **`providers/cosmo_rea6/transform.py`** (`build_month_dataset`,
  `build_annual_dataset`): now retain the real per-cell WGS84
  `latitude`/`longitude` coordinates that cfgrib already decodes from
  the source GRIBs — previously silently dropped by `_strip_scalar_coords`
  before the final dataset was assembled (that function drops *all*
  non-dimension coordinates, not just scalar ones, despite its name).
  **This changes the exported `.nc` schema**: new datasets gain 2-D
  `latitude`/`longitude` coordinates; existing already-processed COSMO-REA6
  archives do not have them retroactively and need reprocessing
  (transform+export only, not re-download) to pick this up.
- **`providers/merra2/transform.py`**: the temperature variable is now
  renamed `T2M` → `T` after Kelvin→Celsius conversion, matching
  ERA5-Land's existing "COSMO emits canonical 'T'; match it" convention.
  **Also changes the exported schema** for the same reason as above.
- **`providers/era5_land/dni_pointwise.py`**,
  **`providers/merra2/dni_pointwise.py`**: `extract_dni_dhi_dirint`/
  `extract_dni_dhi_disc` now delegate to the shared
  `common.dni_reconstruction.reconstruct_dni_dhi` (same public signature
  and behaviour, previously duplicated identically between the two
  providers).
- **`from_csv.CsvWeatherData.reconstruct_dni_from_ghi`**: now delegates to
  the same shared `reconstruct_dni_dhi` function (identical behaviour —
  DISC, apparent zenith, extraterrestrial + GHI-upper clipping — just no
  longer a separate implementation).
- **`pyproject.toml`**: split the previously-unconditional
  `cfgrib`/`dask`/`eccodes`/`pyproj`/`matplotlib`/`xarray`/`netcdf4`/
  `scipy` dependency list into a new `pointquery` extra (`xarray`,
  `netcdf4` — just enough to read already-exported files) and a new
  `pipeline` extra (everything else, for the actual download/transform
  pipeline). Base install is now just `numpy`/`pandas`/`python-dotenv`/
  `requests`/`urllib3`.
- **COSMO-REA6 realigned onto ERA5-Land/MERRA-2's architecture** —
  triggered by investigating a production run whose downloaded/
  decompressed intermediates had been deleted unexpectedly.
  `providers/cosmo_rea6/download.py`/`decompress.py` gained a `months=`
  parameter on `download_all()`/`decompress_all()` (previously
  hardcoded to all 12 months) plus new `verify_downloads()`/
  `verify_decompressed()`; `transform.py` gained `log_dni_stats()`/
  `report_dni_outliers()`; `pipeline.py::run_pipeline(year, months=None,
  ...) -> list[Path]` now does bulk download → bulk decompress →
  per-month transform+export with `resume` support, matching ERA5-Land/
  MERRA-2's shape exactly (previously a simpler, divergent
  single-`build_annual_dataset()` implementation that nothing in
  production actually used). `test_cosmo_one_month.py`/
  `test_cosmo_one_year.py` shrank from 636/840 lines of real pipeline
  logic to ~85/120-line thin CLI wrappers around `run_pipeline()` — no
  pipeline logic lives in `tests/` anymore, matching ERA5-Land/MERRA-2.
  `CosmoREA6Provider`'s `weather run` CLI adapter updated to match
  `ERA5LandProvider`/`MERRA2Provider`'s kwarg-absorption pattern.
  **Breaking CLI change**: COSMO's `--no-cleanup` flag is renamed to
  `--cleanup` (positive, matching ERA5-Land/MERRA-2) across all three
  `test_cosmo_*.py` scripts.
- **`infrastructure/container/docker-compose.yml`/`entrypoint.sh`**: the
  container previously read a separate `COSMO_NO_CLEANUP` variable to
  decide whether to pass `--no-cleanup`, while `docker-compose.yml`
  never actually wired the real `COSMO_CLEANUP` into the container at
  all — inside the container, only the phantom variable had any effect.
  Now passes `COSMO_CLEANUP` straight through; `EnvSettings.
  cosmo_cleanup()` resolves it directly, no container-level flag
  translation needed.

### Fixed

- **COSMO-REA6 per-month cleanup silently orphaned `.idx` files**:
  cfgrib names its index sidecar `<grib_name>.<content-hash>.idx` (e.g.
  `H_SNOW.2D.201801.grb.5b7b6.idx`), not `<grib_name>.idx` as the
  cleanup code assumed — the guessed filename never matched, so `.idx`
  deletion silently no-op'd on every cleanup-enabled run, orphaning it
  once its parent `.grb` was deleted. This is exactly what was found on
  a production server: `decompress/` held only ~70 KB of stray `.idx`
  files, with all `.grb`/`.bz2` correctly gone. Fixed by also globbing
  `<grib_name>.*.idx` (now in `pipeline.py::run_pipeline()`, relocated
  along with the rest of the pipeline logic — see Changed).

## [1.5.2] - 2026-07-26

### Fixed

- **`providers/merra2/downloader.py`**: the full 1980-2025 bulk run
  (552 monthly files) failed exactly 2 of 46 years — 2020 and 2021 —
  on a GES DISC 404. Root cause: `_stream_prefix(year)` hardcodes a
  single "stream" number for the whole 2011-9999 range, but NASA
  reprocessed September 2020 and June-September 2021 under stream 401
  instead of 400. `_fetch` now tries `(primary, primary + 1)` per file,
  using a new `_StreamNotFound` exception (not `OSError`) to skip
  straight to the next stream on a 404 instead of burning 5 backoff
  retries on a permanent failure; transient errors (503, timeouts)
  still retry against the same stream as before. Deliberately not
  hardcoding the affected months as a date range, since GES DISC's
  reprocessing windows are scattered, not contiguous. Verified live:
  re-ran 2020/2021 alone against the fixed downloader, both completed
  clean; the full 46/46-year archive is now verified continuous
  end-to-end via `verify_merra2_months.py` (every year boundary).

### Changed

- **`tests/verify_merra2_months.py`**: widened the `T2M` plausibility
  range from `(-40, 45)` to `(-55, 55)` degC after the full 46-year
  run — the Europe box spans 34N (Saharan margin) to 72N (Arctic
  Scandinavia/Kola), so `-51.09`/`51.72` over 46 years is real climate,
  not corrupted data.
- **`CLAUDE.md`** / **`.claude/merra2/merra2_plan.md`** /
  **`.claude/open.md`** / **`.claude/resolved.md`**: updated to record
  the full 1980-2025 MERRA-2 archive verification and the
  `percentile_index.py` run against the full 46-year archive (36
  output files, genuine per-cell `source_year` diversity: P50 45-46/46
  years per month, P10/P90 32-43/46).
- **`.gitignore`**: added `*_bulk_*.log` (bulk-run scripts can write
  their log outside `data/` if `*_WORK_DIR` isn't set).

---

## [1.5.1] - 2026-07-24

### Fixed

- **`geo/crop.py`**: `crop_netcdf()` failed on every invocation against
  a real `cdo` binary (first live-tested in GitHub Actions CI, not on
  this dev machine — `cdo` has no win-64 conda-forge build).
  `tempfile.mkstemp()` pre-creates the output tmp file, and `cdo`
  refuses to overwrite an existing output file by default — added `-O`
  to the `cdo sellonlatbox` invocation. Also stopped swallowing `cdo`'s
  stderr (`capture_output=True` + `check=True` discarded it, so the CI
  failure showed only "returned non-zero exit status 1" with no
  diagnosable reason) — now logged via `log.error()` before raising.
- **`tests/test_geo_countries.py`**: `test_crop_netcdf_regular_grid`'s
  synthetic dataset now sets CF `units`/`standard_name`/`axis`
  attributes on its `lat`/`lon` coordinates, matching what real
  ERA5-Land/MERRA-2 exports already carry, so the fixture is
  representative of production input.

---

## [1.5.0] - 2026-07-24

### Added

- **MERRA-2: percentile support** —
  `src/weather/providers/merra2/percentile_index.py`, a structural port
  of COSMO/ERA5-Land's KS-distance P10/P50/P90 representative-year
  script.
- **MERRA-2: point-wise DNI/DHI** —
  `src/weather/providers/merra2/dni_pointwise.py`, mirroring
  ERA5-Land's opt-in pvlib DIRINT/DISC decomposition helper (MERRA-2
  only stores GHI in bulk, same as ERA5-Land).
- **`src/weather/geo/`**: new standalone country bbox + NetCDF cropping
  package — `countries.py` (`COUNTRIES` dict, trimmed port of the
  downstream `merra2-energy-pipeline` repo's `countries.py`, ~30
  European countries, no timezones/multilingual aliases), `bbox.py`
  (`BBox([N,W,S,E])` with `.to_area_list()` / `.to_cdo_lonlatbox()`
  converters), `crop.py` (real `cdo sellonlatbox` subprocess cropping,
  atomic tmp-file + rename). CLI: `weather geo {crop,list}`. Works
  today for ERA5-Land/MERRA-2 output NetCDFs; COSMO-REA6 cannot be
  cropped yet (no lat/lon in its production export — see
  `.claude/open.md`). Not wired into any provider's `pipeline.py` by
  design (standalone post-processing step).
- **`common/solar_position.py`**: `spencer_zenith`, shared Spencer
  solar-position formula used by ERA5-Land and MERRA-2 (COSMO keeps its
  own dask-chunked inline version — see `compute_dni`'s docstring for
  why).
- **`common/derived_attributes.py`**: shared pure formulas
  (`wind_speed`, `magnus_rh`, `bolton_rh`, `ghi_from_diffuse_direct`,
  `dni_from_direct`) now single-sourced and imported by every
  provider's own `transform.py`, closing a drift risk where each
  provider used to hand-duplicate this math independently. Also added
  `DNI_ELEVATION_THRESHOLD_DEG` (5 deg elevation, replacing the
  hardcoded `_COS_GUARD` cos(zenith) literal) and an upper cos(zenith)
  bound (`_COS_ZENITH_UPPER = 1.0`) to correct float32 rounding that
  could otherwise push a Spencer-formula cos(zenith) fractionally above
  1.0.
- **`providers/cosmo_rea6/downloaded_attributes.py`**: `RELHUM_2M`
  (relative humidity) wired end-to-end via `role`/`canonical_name`
  fields.
- **`providers/merra2/downloaded_attributes.py`**: `PRECSNOLAND`
  (snowfall) and `SNODP` (snow depth) from the `lnd` collection, now
  appearing in the live-regenerated 2018 data.
- **`tests/compare_providers.py`**: point-wise (DNI/DHI/GHI/T/RH/SF/
  ALBEDO) and whole-Europe domain-stats comparison across all three
  providers' 2018 outputs; xlsx (one sheet/provider) + csv/parquet +
  matplotlib report. Includes a COSMO-only `dni_method_comparison()`
  cross-checking its native exact DNI/DHI against pvlib's exact closure
  formula (NREL SPA zenith) and, for reference, pvlib DIRINT.
- **`tests/verify_merra2_months.py`**: live verification for a full
  year of MERRA-2 output (hour counts, `HH:30` span, cross-month
  continuity, NaN/min-max plausibility).
- **`tests/test_geo_countries.py`**: unit tests for `weather.geo`.
- **Docs**: `docs/BULK_RUN_GUIDE_MERRA2.md`,
  `docs/provider_differences.md` (quantified cross-provider RH/albedo/
  snowfall differences and their physical explanations).
- **`scripts/run_merra2_bulk.sh`**: launch script for a full MERRA-2
  bulk run.
- **`.env.example`**: added `COSMO_CLEANUP`/`ERA5_CLEANUP`/
  `MERRA_CLEANUP` and `*_FROM_YEAR`/`*_TO_YEAR` for all three providers
  — single centralized default per knob, resolved by every
  `run_pipeline()`/`multi_year.py` CLI flag rather than hardcoded per
  entrypoint.
- **`infrastructure/env/weather_env.yml`**: added `openpyxl`
  (`tests/compare_providers.py` xlsx export) and `cdo`
  (`weather.geo.crop` / `weather geo crop`).
- **`pyproject.toml`**: added `excel = ["openpyxl>=3.1"]` extra.

### Changed

- **`providers/cosmo_rea6/transform.py`**: `compute_dni` now imports
  `DNI_ELEVATION_THRESHOLD_DEG` from `derived_attributes.py` instead of
  a separate hardcoded literal; GHI now clips each component (diffuse,
  direct) before summing instead of clipping only the final sum.
- **`providers/cosmo_rea6/export.py`**: minor cleanup alongside the
  `RELHUM_2M` wiring.
- **`providers/era5_land/transform.py`**: RH/wind-speed math now calls
  the shared `magnus_rh`/`wind_speed` formulas instead of its own
  duplicated copies.
- **`providers/era5_land/dni_pointwise.py`** and
  **`providers/merra2/dni_pointwise.py`**: fixed a real cross-provider
  bug via a shared `_align_pressure()` helper — a tz-naive/tz-aware
  pressure index mismatch on `.reindex()` was silently producing
  all-NaN pressure -> all-NaN airmass -> DNI always exactly 0, with no
  exception raised.
- **`providers/merra2/downloader.py`**, **`export.py`**,
  **`pipeline.py`**, **`transform.py`**: adjusted for the `lnd`
  collection (`PRECSNOLAND`/`SNODP`) and stale raw GES DISC global
  attrs no longer leaking through `xr.merge`.
- **`providers/merra2/__init__.py`**: updated for the percentile/
  dni_pointwise additions.
- **`settings.py`**: added the `*_CLEANUP`/`*_FROM_YEAR`/`*_TO_YEAR`
  accessors backing the new `.env.example` knobs.
- **`CLAUDE.md`** and **`.claude/`**: updated task list, per-provider
  context/plan docs, and `open.md` to reflect MERRA-2
  percentile/dni_pointwise and `geo/` as done.
- **`docs/dni_methodology.md`**: expanded with the upper cos(zenith)
  bound fix (sec 5.2) and the pvlib exact-closure / DIRINT comparison
  methodology (sec 11).

### Fixed

- Multi-month `ProcessPoolExecutor` export deadlock in MERRA-2
  (`export.py` now computes each variable before `to_netcdf()`,
  matching ERA5-Land, avoiding dask threaded-write lock contention).
- Stale raw GES DISC global attributes leaking through `xr.merge` in
  MERRA-2 `transform.py`.

---

## [1.4.0] - 2026-07-21

### Added

- **ERA5-Land: full pipeline implementation** — provider is now
  `status: implemented` (was `scaffold`). Added
  `src/weather/providers/era5_land/download.py` (monthly CDS download
  orchestration), `downloader.py` (rewritten CDS request/retry logic),
  `fast_download.py` (parallel multi-connection HTTP range download of
  the CDS result), `transform.py` (GRIB → analysis-ready dataset:
  de-accumulate `ssrd`, Spencer SZA night-mask), `export.py` (monthly
  NetCDF-4, zlib, float32), `pipeline.py` (wires download → transform →
  export for one year), `pipeline_interleaved.py` (overlaps download and
  transform so wall-clock time is `max(download, transform)` instead of
  their sum), `dni_pointwise.py` (opt-in point/region DNI-DHI
  decomposition via pvlib DIRINT/DISC, since ERA5-Land only stores GHI
  in bulk), and `sample_call.py` (reference snippet for the ECMWF
  Datastores client / cdsapi).
- **ERA5-Land: percentile support** —
  `src/weather/providers/era5_land/percentile_index.py`, a structural
  port of COSMO's KS-distance P10/P50/P90 representative-year script.
- **MERRA-2: full pipeline implementation** — provider is now
  `status: implemented` (was `scaffold`). Added
  `src/weather/providers/merra2/download.py` (OPeNDAP download
  orchestration per `(collection, day)`), rewritten `downloader.py`
  (Earthdata/URS session handling), `transform.py` (merges daily
  `rad`/`slv` collections into a monthly dataset, derives GHI/WS_10M/RH),
  `export.py` (monthly NetCDF-4, zlib, float32), and `pipeline.py`
  (wires download → transform → export for one year).
- **CLI** (`src/weather/cli.py`): added `--months` (ERA5-Land subset of
  months), `--ncores` (ERA5-Land/COSMO worker count), `--no-night-mask`
  (disable Spencer SZA night-masking, ERA5-Land only), and `--resume`
  (skip months/years whose output already exists). `_cmd_run` now
  branches on `provider.name` to build provider-specific kwargs, since
  ERA5-Land's `run_pipeline` signature differs from COSMO's.
- **`settings.py`**: added `EnvSettings.merra2_area()` / `era5_area()`
  (CDS-style `"N,W,S,E"` bounding-box parsing, both default to the same
  Europe box `72,-11,34,32`) and `merra2_opendap_max_concurrent()`
  (default 8, since GES DISC has no per-account job queue, unlike CDS).
- **`common/net.py`**: added `_AuthPreservingSession` and
  `build_session(..., preserve_auth_hosts=...)` — re-attaches the
  `Authorization` header across the cross-host redirect chain used by
  NASA Earthdata login, which `requests` strips by default as a CSRF
  precaution.
- **`common/derived_attributes.py`**: added `_era5_rh` (Magnus-formula
  relative humidity from `t2m`/`d2m`) and `_era5_wind_speed`
  (`sqrt(u10**2 + v10**2)`), registered as `ERA5_LAND.RH` and
  `ERA5_LAND.WS_10M` in `DERIVED_FIELDS`.
- **`providers/merra2/downloaded_attributes.py`**: added `COLLECTIONS`
  dict (`rad`/`slv` GES DISC collection names) and an
  `attrs_by_collection()` helper; each attribute entry now tags its
  source `collection`. Replaced `SNODP`/`PRECSNOLAND` with `QV2M`
  (specific humidity, feeds the RH formula) and `ALBEDO`.
- **Docs**: `docs/BULK_RUN_GUIDE_ERA5-LAND.md`, `docs/DOWNLOAD_AND_LOGGING.md`,
  `docs/MERRA2_PIPELINE_GUIDE.md`.
- **Tests/tools**: ERA5-Land pipeline runners (`test_era5_one_month.py`,
  `test_era5_one_year.py`, `test_era5_multi_year.py`), MERRA-2 pipeline
  runners (`test_merra2_one_month.py`, `test_merra2_one_year.py`,
  `test_merra2_multi_year.py`), ERA5-Land boundary/diagnostic tools
  (`repair_month_boundaries.py`, `verify_months.py`,
  `check_boundary_steps.py`, `check_first_hour.py`, `diagnose_nc.py`,
  `enumerate_month.py`, `inspect_era5_eccodes.py`, `inspect_era5_grib.py`),
  and `audit_imports.py` (lint tool enforcing global-imports-only).
- **`scripts/run_era5_bulk.sh`**: launch script for a full ERA5-Land
  bulk run.
- **`CLAUDE.md`** and **`.claude/`**: added the persistent project
  context file and per-provider `.claude/{cosmo_rea6,era5_land,merra2}/`
  context/plan docs, plus `.claude/open.md` / `resolved.md` issue
  tracking.

### Changed

- **`providers/README.md`**: `base_percentile.py` documented as dead
  code — the template-method P10/P50/P90 design was superseded by the
  standalone `percentile_index.py` scripts now used by both COSMO-REA6
  and ERA5-Land.
- **`providers/era5_land/__init__.py`** / **`providers/merra2/__init__.py`**:
  now thin façades over their `pipeline.run_pipeline`;
  `validate_environment()` checks real package imports and credentials
  (CDS `~/.cdsapirc` / Earthdata auth) instead of a static message;
  `run_pipeline()` translates/drops COSMO-only CLI kwargs rather than
  erroring.
- **`providers/era5_land/config.py`**: added `area`, `cds_max_concurrent`,
  `cds_max_retries`, `download_connections` to the resolved config dict.
- **`providers/merra2/config.py`**: added `area` and
  `opendap_max_concurrent`.
- **COSMO-REA6**: default `--complevel` (zlib compression) changed from
  `5` to `1` in `cli.py` and `providers/cosmo_rea6/pipeline.py`, trading
  file size for faster writes; `export.py`/`naming.py` docstrings
  updated from stale `buem.weather.*` import paths.
- **`.env.example`**: rewritten to cover all three providers — added
  `ERA5_DATA_FORMAT`, `ERA5_CDS_MAX_CONCURRENT`, `ERA5_USE_ARIA2`,
  `ERA5_CDS_URL`/`ERA5_CDS_KEY`, `ERA5_AREA`,
  `EARTHDATA_USERNAME`/`PASSWORD`, `MERRA2_OPENDAP_MAX_CONCURRENT`,
  `MERRA2_AREA`.
- **`infrastructure/env/weather_env.yml`**: added `ecmwf-datastores-client`
  and `aria2` (parallel multi-connection downloader); pinned
  `python=3.12.*` explicitly.
- **`scripts/common.sh`**: `.env` is now stripped of `\r` before
  `source`, so a CRLF-saved `.env` no longer breaks on the Linux server.
- **`.gitignore`**: added `deploy_to_server.ps1`.
- **`LICENSE`**: copyright year range updated `2024-2026` → `2025-2027`.
- **`src/weather/tests/README.md`**: rewritten with a category table
  (pytest units vs. pipeline runners vs. diagnostic tools) covering the
  new ERA5-Land/MERRA-2 runners and tools.

### Fixed

- **`common/derived_attributes.py`**: `ERA5_LAND` registry no longer
  advertises `DHI`/`DNI` (those require a per-site DIRINT/DISC
  decomposition that cannot broadcast over a `(time, y, x)` grid — moved
  to the opt-in `dni_pointwise.py` helper); `test_derived_attributes.py`
  updated to match (`GHI`/`RH`/`WS_10M` for ERA5-Land).
- Lint/type fixes across new diagnostic scripts under `src/weather/tests/`
  (`audit_imports.py`, `check_first_hour.py`, `enumerate_month.py`,
  `inspect_era5_grib.py`) and `providers/era5_land/pipeline_interleaved.py`
  to satisfy `ruff check src/` and `mypy src`.
- **`providers/era5_land/sample_call.py`**: removed a module-level
  `client.check_authentication()` network call and hardcoded local
  Windows paths (`D:/test/...`) from download targets.
- **`providers/{cosmo_rea6,era5_land}/percentile_index.py`**: mypy
  `attr-defined`/type-conflict errors on the optional numba `prange`
  import — a dead fallback reassignment (`prange = range` in the
  `except` branch) conflicted with numba's real `prange` type when
  numba is installed; removed it and silenced the remaining call site
  with a targeted `# type: ignore[attr-defined]` (numba's stub doesn't
  type `prange` as iterable), verified locally against the same numba
  version CI installs.

---

## [1.3.0] - 2026-06-28

### Added

- Included a docs folder with the following files:
  `debugging.md`, `dni_methodology.md`, `git-push-workflow.md`,
  `parallelization.md`, `percentile_methodology.md`, and `qa.md`.
- `./src/weather/providers/cosmo_rea6/percentile_index.py` — calculates
  percentiles (P10, P50, and P90) using the FH method and GHI as the
  basis for each cell and each month.

### Changed

- Added versions to python packages in weather_env.yml.
- Added `relative humidity` attribute to `downloaded_attributes.py`
  of `cosmo-rea6`, `era5_land`, and `merra2` submodules.
- Minor updates to .gitignore, CONTRIBUTING.md, and README.md

---

## [1.2.0] — 2026-06-15

### Added

- `./infrastructure/container/entrypoint.sh` — routes to the appropriate
  pipeline script based on the PIPELINE_MODE environment variable.
- `./src/weather/common/derived_attributes.py` — Cross-provider irradiance
  derivation (GHI, DHI, DNI).
- `./src/weather/common/merge.py` — NetCDF-4 / HDF5 monthly-to-annual merge
  utilities.
- `./src/weather/common/parallel.py` — Shared thread-pool executor for
  I/O-bound parallel tasks.
- `./src/weather/common/percentile_poe.py` and `./src/weather/common/percentile.py`
  — different method to calculate weather percentile years with P10, P50, and
  P90 representation.
- `./src/weather/providers/merra2/` — Scafolding addition related to MERRA2
  with config.py, downloaded_attributes,py, downloader.py, main.py,
  base_decompressor.py, base_downloader.py, base_percentile.py,and
  corresponding README.md files addition.
- `./src/weather/providers/era5_land/` — Scafolding addition related to
  config.py, downloaded_attributes,py, and downloader.py similar to
  MERRA2.
- `./src/weather/tests/` — added multiple test files to test processing
  of one month, one year, multi-year, and percentile weather data
  processing along with integration pipeline testing.
- `./src/weather/settings.py` — provides a centralized environment for
  the entire weather pipeline.
- `setup.sh` — creates or updates the conda environment and installs
  the package in editable mode.

### Changed

- **workflows**: addition of "on" event that triggers workflow. For
  ci.yml, this should be triggered when there is a push event on the
  `main` and `develop` branches. In release.yml, this is triggered with
  a tag starting with `v`.
- **container**: changes to `Dockerfile` and `docker-compose.yml`
  to consider single and multi-year weather data processing.
- **`weather_env.yml`**: addition of `Jupyter`, `matplotlib=3.10.*`,
  `lbzip2` and `h5py=3.11.*`.
- **scripts**: multiple files adjusted for full year processing.
  Still need to be adjusted for multi-year processing.
- **`/src/weather/common/`**: ``cleanup.py` updated to adjust the
  cleanup pattern of downloaded and decompressed files. `download.py`
  added functions to compute- and save SHA256 checksum of a file.
  Finally, `env.py` adjusted loading of environment files.
- **`/src/weather/providers/cosmo_rea6/`**: changes to the pipeline
  related to parallel processing, multi-year processing, and making
  download of attributes modular.
- **`/src/weather/providers/<merra2/era5_land>`**: corresponding
  `__init__.py` update related to environment path definition.
- **`setup.ps1` / `setup.bat`**: `conda develop src` replaced with
  `conda run -n $EnvName pip install -e .`; `CONDA_BLD_PATH` configuration
  removed.
- **environment**: `.pyproject.toml` and `meta.yaml` update related to
  pip install due to addition of new python packages. `MD018` set to
  `false` in `.markdownlint.json`.

---

## [1.1.0] — 2026-05-19

### Added

- `.github/workflows/ci.yml` — automated lint (ruff), type-check (mypy),
  pytest with coverage, and CLI smoke test on every push/PR.
- `.github/workflows/release.yml` — build sdist + wheel and publish to GitHub
  Releases automatically on `v*` tag push.
- `.github/agents/uu-buem-align.agent.md` — VS Code Copilot custom agent for
  UU-BUEM cross-repo standardisation workflows.
- `.markdownlint.json` — MD013 (100-char lines, tables exempt) and MD024
  (siblings-only duplicate headings); aligned with `UU-BUEM/occupancy`.
- `.yamllint.yml` — excludes `meta.yaml` from YAML linting (Jinja2 templates
  are pre-processed by conda-build before YAML parsing).
- `.vscode/settings.json` — classifies `meta.yaml` as `jinja` language to
  suppress cascading false-positive YAML errors in VS Code.
- `setup.bat` — restored missing Windows cmd.exe setup script; mirrors
  `setup.ps1` behaviour.
- Post-install verification step (`python -m weather info`) in `setup.ps1`
  and `setup.bat`.
- OCI `LABEL` metadata (`source`, `description`, `licenses`) in `Dockerfile`.

### Changed

- **Python**: requirement lowered from `>=3.14` (pre-release) to `>=3.12`
  across `pyproject.toml`, `meta.yaml`, `weather_env.yml`, ruff
  `target-version`, and mypy `python_version`. Python 3.14 can be
  re-targeted once conda-forge packages have stable 3.14 builds.
- **Dependencies**: all core deps pinned with minimum (and where appropriate
  upper) version bounds in `pyproject.toml`; optional extras pinned too
  (`pvlib>=0.10`, `pyarrow>=15.0`, `cdsapi>=0.7`); dev extras given minimum
  versions (`mypy>=1.8`, `pytest>=7.4`, `pytest-cov>=4.1`, `ruff>=0.6`).
- **`weather_env.yml`**: all packages pinned for reproducibility
  (`numpy=1.26.*`, `pandas=2.2.*`, `scipy=1.13.*`, etc.); `conda-build`
  removed (no longer needed); `pip: -e .` added so the package is installed
  editable on `conda env create`.
- **`conda_build_config.yaml`**: added `pandas: 2.2` and `python: 3.12` pins.
- **`meta.yaml`**: version now derived from git tag via Jinja2
  `GIT_DESCRIBE_TAG`; `python >=3.14` → `>=3.12`; optional extras
  (`cdsapi`, `pvlib`, `pyarrow`) removed from mandatory `run` deps; explicit
  version bounds added to all run dependencies; `test.imports` expanded to
  include `weather.providers`.
- **`setup.ps1` / `setup.bat`**: `conda develop src` replaced with
  `conda run -n $EnvName pip install -e .`; `CONDA_BLD_PATH` configuration
  removed.
- **Docker**: base image pinned `continuumio/miniconda3:latest` →
  `:24.1.2-0` in both `Dockerfile` and `weather.def`.
- **`docker-compose.yml`**: image tag `weather:latest` →
  `weather:${WEATHER_VERSION:-latest}`.
- **`pyproject.toml`**: `write_to` → `version_file` (setuptools-scm ≥ 8
  non-deprecated API); `fallback_version` set to `1.1.0`.
- **`_version.py`**: added `version`, `version_tuple`, `__commit_id__`,
  `commit_id` aliases to match occupancy format.
- **`__init__.py`**: `_version` import wrapped in `try/except ImportError`
  with `"1.1.0"` fallback — prevents import failure in source-only installs.
- **`.gitignore`**: `.vscode/settings.json` (wrongly ignored) changed to
  `.vscode/*` + `!.vscode/settings.json` so workspace settings are tracked.
- **`CONTRIBUTING.md`**: removed `conda develop` references; fixed MD013
  line-length violation.

### Fixed

- `from_csv.py`: removed `Path(__file__).parents[2]` path hack (now requires
  an explicit absolute path); fixed deprecated `'H'` → `'h'` pandas resample
  alias; guarded Feather/pyarrow cache behind `try/except ImportError`;
  removed `if __name__ == "__main__"` debug block with hardcoded Windows path.

---

## [1.0.0] — 2026-05-18

### Added

- Provider-based architecture: `cosmo-rea6` (implemented), `merra-2` and
  `era5-land` scaffolds.
- `src/` layout with `src/weather/` package and proper `pyproject.toml`.
- `common/` module with shared download (`download.py`), decompression
  (`decompress.py`), and HTTP/auth utilities (`net.py`).
- `cli.py` for structured CLI commands (`info`, `validate`, `run`).
- Lazy provider registry to avoid eager import of all provider modules.
- Docker multi-stage `Dockerfile` + `docker-compose.yml` for local dev.
- Apptainer definition `weather.def` for HPC (Snellius/SLURM).
- `pyproject.toml` with dynamic versioning from `_version.py`.
- `setup.ps1` / `setup.bat` for one-command Windows dev environment setup.

### Changed

- Renamed `Dockerfile.weather` → `Dockerfile`; image tag `buem-weather` →
  `weather`.
- Renamed `buem_weather.sif` → `weather.sif` in all scripts.
- Set `python=3.14` in `weather_env.yml`; removed `lbzip2` (OS package,
  not conda).
- Updated `from_csv.py` path resolution from `buem_root` to repo root.

### Removed

- Stale `buem` references throughout container files and shell scripts.

---

## [0.1.0] — 2026-05-13

### Initial Release

- Initial extraction from the `buem` monorepo.
- COSMO-REA6 download → decompress → transform → export pipeline.
- Shell scripts for Snellius HPC: `setup_env.sh`, `run_pipeline.sh`,
  `run_pipeline_container.sh`, `build_container.sh`.
