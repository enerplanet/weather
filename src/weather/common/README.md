# `weather/common/` — Shared Utilities

This folder contains modules that are **used by more than one provider**
(COSMO_REA6, MERRA2, ERA5_LAND) or by the pipeline scripts themselves.
Code that is specific to a single provider lives under `providers/<name>/`
instead.

---

## Module index

| File | Purpose | Why it is here and not in a provider folder |
| --- | --- | --- |
| `__init__.py` | Package marker; exposes `download`, `decompress`, `net` | — |
| `cleanup.py` | Delete intermediate files after a pipeline phase | Used by all providers and test scripts |
| `decompress.py` | bz2 decompression with lbzip2/pbzip2 fallback | COSMO-REA6 and MERRA-2 both use bz2-compressed archives |
| `download.py` | Atomic HTTP download with retry logic | All providers download from remote servers |
| `env.py` | Read `COSMO_*` environment variables with typed defaults | Central configuration point; avoids scattered `os.environ` calls |
| `net.py` | `requests.Session` with retry / timeout wrappers | Shared by download and any future API client |
| `validate.py` | File-integrity checks (exists, non-empty, GRIB magic bytes) | Used by test scripts after download and decompress |
| `parallel.py` | `ThreadPoolExecutor` wrapper (`run_parallel`) | Download and decompress parallelism is the same pattern for all providers |
| `merge.py` | `NetCDFMerger` — h5py-based monthly → annual NetCDF merge | Post-processing step used after any provider's monthly output |
| `derived_attributes.py` | GHI/DHI/DNI formula registry; `apply_derived_fields` dispatcher | **All three providers** use the same irradiance formulas — see note below |

---

## Why is `derived_attributes.py` here?

The irradiance derivation formulas (GHI = `SWDIFDS_RAD + SWDIRS_RAD`,
Spencer solar-position, pvlib DIRINT) apply to **all three providers** that
produce radiation output:

- **COSMO_REA6** — gridded 824×848 vectorised NumPy/Dask
- **MERRA2** — per-point pvlib DIRINT time series
- **ERA5_LAND** — per-point pvlib DIRINT time series

`apply_derived_fields(provider_name, dataset)` dispatches to the correct
implementation for each provider.  If this file lived in `providers/cosmo_rea6/`,
both MERRA-2 and ERA5-Land would have to import from a sibling provider's
folder — a circular/fragile dependency.  `common/` is the correct location.

---

## Why is `merge.py` here?

`merge.py` is a **post-processing utility** that operates on any NetCDF
files, not just COSMO-REA6.  Placing it in `cosmo_rea6/` would be
misleading.  It has no dependency on GRIB reading, coordinate transformations,
or any provider-specific logic.

`NetCDFMerger` uses h5py directly rather than `xr.open_mfdataset` to avoid
full in-memory decompression of each variable and to prevent `NetCDF: Not a
valid ID` errors on GPFS network file systems.  See [docs/debugging.md §4](
../docs/debugging.md#4-netcdf-not-a-valid-id-during-merge-or-close) for
the full explanation.

---

## Why is `parallel.py` here?

`ThreadPoolExecutor` with task tracking and structured logging is duplicated
in every provider's `download_all()` and `decompress_all()`.  Extracting it
to `common/parallel.py` means:

- Adding a new provider requires zero boilerplate for parallelism.
- Progress logging is consistent across all providers.

---

## Data flow through `common/`

```text
Phase 1 — Download
  net.py ──► download.py ──► parallel.py ──► provider/download.py
                                   │
                         validate.py (post-download check)

Phase 2 — Decompress
  decompress.py ──► parallel.py ──► provider/decompress.py
                                   │
                         validate.py (post-decompress check)

Phase 3 — Transform (per provider)
  derived_attributes.py ──► provider/transform.py
  env.py ──► provider/config.py

Post-processing
  merge.py (monthly NCs → annual NC)
  provider/percentile_index.py (standalone, see providers/README.md)

Cleanup (between phases)
  cleanup.py
```
