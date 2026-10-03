# MERRA-2 pipeline guide

## What this pipeline does

Downloads MERRA-2 reanalysis data from NASA GES DISC via Earthdata Cloud
**OPeNDAP**
(server-side subsetting — no global file is ever downloaded), cropped to
a fixed Europe bounding box (same footprint as ERA5-Land:
`N,W,S,E = 72,-11,34,32`), and produces one monthly NetCDF-4 file per
year (`MERRA2_<YYYY>_<MM>_all_attrs.nc`), matching ERA5-Land's output
convention.

MERRA-2 coverage: **1980 to present**.

## Collections and attributes

Three GES DISC collections cover the 13 downloaded attributes (one
OPeNDAP request per collection per day):

| Collection             | Short key | Attributes                                             |
|-------------------------|-----------|----------------------------------------------------------|
| `M2T1NXRAD.5.12.4`      | `rad`     | `SWGDN` (-> GHI), `ALBEDO`                                |
| `M2T1NXSLV.5.12.4`      | `slv`     | `T2M`, `QV2M`, `U2M`, `V2M`, `U10M`, `V10M`, `U50M`, `V50M`, `PS` |
| `M2T1NXLND.5.12.4`      | `lnd`     | `SNODP`, `PRECSNOLAND`                                    |

**Why `M2T1NXLND` (SNODP/PRECSNOLAND) and `U50M`/`V50M` were added.**
This was originally scoped out — a 3rd collection means one more
OPeNDAP request per day, and `ALBEDO` (free within the already-fetched
`rad` request) was judged more immediately useful. That changed once a
confirmed downstream consumer
([merra2-energy-pipeline](https://github.com/THD-Spatial-AI/merra2-energy-pipeline),
`src/data_pipeline/config.py`) specified exactly what its PV and wind
models need: `SNODP`/`PRECSNOLAND` for PV snow-loss derating, and
`U50M`/`V50M` (hub-height wind, more relevant to turbines than 10 m)
for the wind model. `U50M`/`V50M` came free within the existing `slv`
request (same collection as `U10M`/`V10M`); `lnd` is the one genuinely
new per-day request. `PRECSNOLAND` arrives as `kg/m^2/s` and is
converted to `kg/m^2/h` in `transform._convert_units`, matching
COSMO's `SNOW_GSP`+`SNOW_CON` / ERA5-Land's `sf` convention.

`build_monthly_dataset()`'s signature changed from `(rad_paths,
slv_paths, *, year, month)` to `(rad_paths, slv_paths, lnd_paths, *,
year, month)` — a breaking change for any direct caller; the pipeline's
own caller (`pipeline.py`) was updated. **Verified live**: all 12
months of 2018 re-downloaded/re-transformed against real GES DISC data
(`lnd` collection included) and checked with `verify_merra2_months.py`
— correct hour counts, `HH:30` span, no gaps, `SNODP`/`PRECSNOLAND`/
`U50M`/`V50M` all populated with plausible values.

### A real bug found during this rerun: `export_netcdf` always skipped

`export_netcdf()` had its own unconditional `if output_path.exists():
skip` check, completely independent of `pipeline.py`'s `skip_existing`/
`resume` parameter. The first live rerun after adding the `lnd`
collection reported "OK" for all 12 months while silently writing
**zero bytes** — the old 2018 output files (from before `lnd` existed)
were already present on disk, so every write was skipped regardless of
`--resume` being passed or not. Fixed by removing the internal check
from `export_netcdf` in all three providers' `export.py` — the
skip/overwrite decision belongs solely to the caller's already-correct
`resume`-gated logic. If you need to force a real regeneration and
still see "already exists, skipping" in old logs from before this fix,
delete the stale output file(s) first, then re-run.

## Authentication (NASA Earthdata)

1. Register at <https://urs.earthdata.nasa.gov/> (free).
2. In *Applications > Authorized Apps*, add **"NASA GESDISC DATA
   ARCHIVE"** to your approved list.
3. Either set in `.env`:

   ```text
   EARTHDATA_USERNAME=<your_username>
   EARTHDATA_PASSWORD=<your_password>
   ```

   or create `~/.netrc`:

   ```text
   machine urs.earthdata.nasa.gov
       login  <your_username>
       password <your_password>
   ```

   (`chmod 600 ~/.netrc`). Resolution order is env vars first, then
   `~/.netrc` — see `weather.common.net.earthdata_auth`.

### Why a custom session is needed

NASA's login flow redirects across hosts (`urs.earthdata.nasa.gov` <->
the Earthdata Cloud OPeNDAP host `opendap.earthdata.nasa.gov`). `requests`
strips the `Authorization` header on cross-host redirects by default (a
security precaution), which breaks this specific flow. `weather.common
.net.build_session(..., preserve_auth_hosts={...})` re-attaches the
header for exactly these two trusted hosts — see
`Merra2Downloader._get_session`.

## Spatial cropping (OPeNDAP constraint expressions)

MERRA-2's native grid is fixed: 0.5° latitude x 0.625° longitude, global
origin at -90/-180. `Merra2Downloader._bbox_indices` converts the
configured `[N, W, S, E]` box (`MERRA2_AREA` env var, default the Europe
box above) to inclusive grid-index ranges via:

```text
lat_idx = round((lat_deg - (-90)) / 0.5)
lon_idx = round((lon_deg - (-180)) / 0.625)
```

Those indices are embedded in the request URL as a DAP4 constraint
(shown unencoded), e.g.:

```text
https://opendap.earthdata.nasa.gov/collections/C1276812851-GES_DISC/granules/M2T1NXRAD.5.12.4%3AMERRA2_400.tavg1_2d_rad_Nx.20180101.nc4.dap.nc4
    ?dap4.ce=/SWGDN[0:1:23][248:1:324][270:1:340];/ALBEDO[0:1:23][248:1:324][270:1:340];/time;/lat[248:1:324];/lon[270:1:340]
```

**Known limitation — no cross-provider regridding.** MERRA-2's native
grid (0.5°x0.625°) is coarser than and not cell-aligned with
ERA5-Land's (0.1°x0.1°). This pipeline crops to the *same geographic*
box on MERRA-2's *own* grid — it does not interpolate/regrid onto
ERA5-Land's grid. Cross-provider regridding, if ever needed, is a
separate future task.

## Timestamp convention (does NOT align with COSMO/ERA5-Land)

MERRA-2's `tavg1_2d` collections (`rad`, `slv`) report true hourly
time-averages labeled at the **midpoint** of the averaging interval, so
every timestamp lands on `HH:30` (e.g. `2018-03-01T00:30`, `T01:30`, …
`T23:30`) — confirmed against a live 2018-03 smoke test. ERA5-Land and
COSMO-REA6 both label on the hour (`HH:00`). This is a genuine
provider difference, not a bug: shifting MERRA-2's index to `HH:00`
would mislabel the averaging window it actually represents.

**This is left as-is by design.** Do not silently relabel MERRA-2
timestamps to force alignment. Any code that merges/concatenates
MERRA-2 with COSMO/ERA5-Land data on a shared time index must handle
the 30-minute offset explicitly (e.g. resample/interpolate one series
onto the other's index), rather than assuming the raw indices match.

## Derived fields: GHI, wind speed, relative humidity

### GHI = SWGDN directly (no de-accumulation)

Unlike ERA5-Land's accumulated `ssrd` (which needs de-accumulation and
month-boundary repair — see `providers/era5_land/boundary_repair.py`),
MERRA-2's
`SWGDN` is an **instantaneous** flux. GHI is simply `SWGDN`,
night-masked via `weather.common.derived_attributes.apply_derived_fields
(ds, "MERRA2", sol_pos, times, fields=["GHI"])`. No boundary bookkeeping
is needed at all.

### DNI/DHI are NOT computed in bulk

`common/derived_attributes.py`'s `DERIVED_FIELDS["MERRA2"]` registry
also defines `DHI`/`DNI` formulas via pvlib's DIRINT algorithm — but
DIRINT operates on a 1-D time series per site and cannot broadcast over
a full `(time, y, x)` grid. This is the same limitation that made
ERA5-Land defer DNI/DHI to an opt-in point-wise helper
(`era5_land/dni_pointwise.py`) rather than compute it in bulk. MERRA-2's
`transform.py` therefore only requests `fields=["GHI"]` from the
registry. A `merra2/dni_pointwise.py` mirroring ERA5-Land's is a
documented future extension, not built in this phase.

### Wind speed

`WS_10M = sqrt(U10M**2 + V10M**2)`, same convention as COSMO/ERA5-Land.

### Relative humidity — specific-humidity-based (new formula)

Unlike COSMO (direct `RELHUM_2M`) or ERA5-Land (dew-point Magnus
formula), MERRA-2 has no direct RH or dew-point field — only `QV2M`
(specific humidity at 2 m). RH is derived from `QV2M` + `T2M` + `PS`
using the standard specific-humidity -> vapor-pressure conversion,
combined with the Bolton (1980) saturation-vapor-pressure curve:

```text
e  = QV2M * PS / (0.622 + 0.378 * QV2M)          # vapor pressure, Pa
es = 611.2 * exp(17.67 * T / (T + 243.5))        # Bolton 1980, Pa (T in degC)
RH = 100 * e / es, clipped to [0, 100]
```

Implemented in `weather.providers.merra2.transform._compute_rh`.

**Important ordering note:** unlike ERA5-Land's Magnus RH (which needs
*pre-conversion* Kelvin inputs, computed before the K->degC step), this
Bolton formula needs `T2M` in **Celsius** — so `_compute_rh` runs
**after** the unit-conversion step, the reverse of ERA5-Land's ordering.
This is called out explicitly in the `transform.py` docstring so a
future maintainer doesn't copy ERA5-Land's ordering blindly.

## Running the pipeline

```bash
# Single month smoke test
python src/weather/tests/test_merra2_one_month.py --year 2018 --month 1

# Single year, all 12 months
python src/weather/tests/test_merra2_one_year.py --year 2018 --ncores 8
```

### Bulk multi-year run (`sd26`, tmux)

Before starting, on `sd26`:

1. **Sync code.** This session's fixes (the `lnd`-collection live
   verification, the `export_netcdf` skip-if-exists bug fix, and the
   `COSMO_CLEANUP`/`ERA5_CLEANUP`/`MERRA_CLEANUP` centralization) are
   local, uncommitted changes on the dev machine as of this writing —
   commit + push, then `git pull` on `sd26`, before starting a fresh
   1980-2025 run. Running without the `export_netcdf` fix means any
   month whose output file already exists (e.g. from a prior partial
   attempt) will silently fail to update, exactly the bug this session
   found.
2. **Check credentials.** `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD` (or
   `~/.netrc`) must be set up on `sd26`, not just this dev machine.
3. **Check disk.** MERRA-2's full 1980-2025 footprint is modest — the
   2018 year alone was ~1 GB of monthly output; 46 years is on the
   order of tens of GB, trivial against `sd26`'s 15 TB. No need for
   `--cleanup`; the new default (`MERRA_CLEANUP=false`) keeps daily
   files, which is what you want anyway per the same reasoning as the
   COSMO cleanup discussion.

```bash
tmux new -s merra2
conda activate weather_env
cd /path/to/weather   # your sd26 checkout
git pull

export MERRA_WORK_DIR=/data/soma/merra2
export MERRA2_AREA=72,-11,34,32
mkdir -p logs

python src/weather/tests/test_merra2_multi_year.py \
    --from-year 1980 --to-year 2025 \
    --ncores 80 --parallel-years 6 \
    --resume \
    2>&1 | tee -a "logs/merra2_multi_year_$(date +%Y%m%d_%H%M%S).log"

# detach: Ctrl-b then d      reattach: tmux attach -t merra2
```

Notes on the flags:

- **`--resume`** is safe to leave on for every invocation, including
  the first — it only skips a *year* whose all-12-months output already
  exists (`_all_monthly_exist()`), so it's the right default for both
  a fresh start and any later restart after an interruption.
- **`--ncores 80 --parallel-years 6`** leaves headroom on `sd26`'s 94
  cores for other users/jobs, and divides into ~13 cores/year — close
  to the useful ceiling, since each year's transform phase only ever
  parallelizes across 12 months (`ProcessPoolExecutor` caps at
  `min(ncores_per_year, 12)`; cores beyond that per year are wasted, not
  harmful). Push `--parallel-years` down (e.g. to 4) if `sd26` is shared
  with other work at the time, since 6 years running concurrently each
  also issue up to `MERRA2_OPENDAP_MAX_CONCURRENT` (default 8) requests
  against GES DISC — 6x8=48 concurrent requests is probably fine (no
  CDS-style per-account queue) but has not been tested at this scale;
  watch the first hour of logs for repeated timeouts/retries before
  trusting it unattended overnight.
- **No `--cleanup`** — matches the new centralized default (keep
  everything; see `.claude/open.md`'s cleanup-centralization entry).
- The `tee` redirect keeps a persistent, timestamped log outside the
  tmux scrollback, so `tmux attach` isn't the only way to check
  progress or diagnose a failure after the fact.
- Runtime estimate: the 2018 rerun this session (`lnd`-only download,
  rad/slv already cached) took ~173 s. A **fresh** year (all 3
  collections, ~1095 daily files) will take meaningfully longer —
  budget roughly 8-10 minutes/year sequentially as a rough planning
  number; `--parallel-years 6` should cut wall-clock time
  substantially, though the actual speedup depends on how GES DISC
  responds to sustained concurrent load, which hasn't been measured at
  this scale. Consider a small trial first (e.g. `--from-year 2015
  --to-year 2018`) to sanity-check throughput before committing to the
  full 46-year range unattended.

Unlike ERA5-Land's CDS queue (effectively 1 job/account), GES DISC's
OPeNDAP server has no per-account job queue — `MERRA2_OPENDAP_MAX_CONCURRENT`
(default 8) can be raised more freely than ERA5's `ERA5_CDS_MAX_CONCURRENT`,
though `--parallel-years` still divides transform cores the same way.

### Fixed: multi-month export deadlock

Running more than one month's transform+export concurrently (the
normal case for `test_merra2_one_year.py`/`test_merra2_multi_year.py`
with `ncores` > months-in-flight) used to be able to hang indefinitely.
Root cause: `export.py` called `ds.to_netcdf()` directly on the lazy,
`xr.open_mfdataset`-backed dataset, which hands the write to dask's
default *threaded* scheduler — spawning `ncores` threads to write a
**single** file, all serializing on xarray's netCDF4 write lock. With
multiple sibling `ProcessPoolExecutor` worker processes each doing this
for their own file at the same time, this could deadlock (confirmed via
`py-spy dump`: every dask worker thread stuck at
`xarray.backends.locks.CombinedLock.__enter__`, zero progress for 30+
minutes). Fixed by materialising each variable with `.compute()` one at
a time before `to_netcdf()` — the same pattern ERA5-Land's exporter
already used. If you see a `test_merra2_one_year.py` run stall with no
log output for several minutes and elevated but static process CPU
time, that is this bug; make sure you're on a `weather` checkout at or
after this fix.
