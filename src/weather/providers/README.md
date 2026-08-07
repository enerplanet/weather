# `weather/providers/` — Data-Provider Implementations

This folder contains one sub-package per weather data source plus the
abstract base classes that define the shared provider contract.

---

## Sub-packages

| Sub-package | Data source | Status |
| --- | --- | --- |
| `cosmo_rea6/` | DWD COSMO-REA6 reanalysis (1986–2018, 6 km grid over Central Europe) | Production-ready |
| `merra2/` | NASA MERRA-2 reanalysis (global, ~55 km) | Stub / in development |
| `era5_land/` | Copernicus ERA5-Land reanalysis (global, 9 km) | Stub / in development |

---

## Base classes

| File | Pattern | Purpose |
| --- | --- | --- |
| `base.py` | `Protocol` | Minimal typing interface used by the CLI registry (`WeatherProvider`) |
| `base_downloader.py` | Abstract base class | Template-method download skeleton: `is_complete → skip / _fetch` |
| `base_decompressor.py` | Abstract base class | Template-method decompress skeleton: skip-if-done logic |

### Adding a new provider

1. Create `providers/<name>/` with `__init__.py`.
2. Implement `download.py`, `decompress.py`, `transform.py`, `pipeline.py`
   extending the corresponding base classes.
3. Register the provider name in `weather/registry.py`.
4. Add `percentile_index.py` following the pattern below.

---

## `percentile_index.py` — the actual production pattern (COSMO + ERA5-Land)

Each of `cosmo_rea6/percentile_index.py` and `era5_land/percentile_index.py`
is a self-contained, three-phase script (no shared base class):

```text
Load (parallel file read)  →  KS distance match per month/cell  →  Mosaic
(day-summed GHI, leap        (argmin |empirical CDF − pooled       (spawn
 days dropped)                 P10/P50/P90 threshold|)              workers
                                                                     write
                                                                     36 NC
                                                                     files)
```

Both read monthly NetCDF files directly (`*_YYYY_MM_*.nc`), find — per
grid cell and calendar month — the year whose empirical GHI distribution
best matches the pooled P10/P50/P90 threshold (Finkelstein-Schafer
KS-distance), then mosaic every variable from the winning year into
`{provider}_{p10,p50,p90}_{MM}_all_attrs.nc`, including a `source_year`
provenance variable. ERA5-Land's version is a structural port of COSMO's,
differing only in: filename regex, grid size inferred from data (not
hardcoded), and `n_cpu_cores` default (6, I/O-bound vs COSMO's 94,
CPU-bound).

---

## COSMO-REA6 sub-package (`cosmo_rea6/`)

| File | Purpose |
| --- | --- |
| `__init__.py` | Exposes the provider class |
| `config.py` | Resolves all `COSMO_*` environment variables to typed paths/values |
| `download.py` | Generates DWD OpenData URLs; drives `BaseDownloader` |
| `downloader.py` | Concrete `BaseDownloader` subclass for DWD HTTPS downloads |
| `downloaded_attributes.py` | Enum/list of the 9 available raw attributes |
| `decompress.py` | bz2 → GRIB with lbzip2/pbzip2/python-bz2 fallback |
| `decompressor.py` | Concrete `BaseDecompressor` subclass |
| `transform.py` | Opens GRIB with cfgrib; applies derived fields; writes monthly NC |
| `export.py` | `xr.Dataset → NetCDF` with zlib encoding and attribute metadata |
| `naming.py` | Canonical filename helpers for all output paths |
| `pipeline.py` | Orchestrates Phases 1 → 2 → 3 for one year; returned by `WeatherProvider.run_pipeline()` |
| `percentile_index.py` | Standalone P10/P50/P90 KS-distance script (see above) |

### COSMO-REA6 per-year pipeline flow

```text
pipeline.py
│
├── Phase 1: Download (parallel)
│   download.py × N attributes × 12 months
│   ─ atomic HTTP GET to DWD OpenData
│   ─ skip if bz2 already present and valid
│
├── Phase 2: Decompress (parallel)
│   decompress.py × N attributes × 12 months
│   ─ lbzip2 / pbzip2 / python-bz2 fallback
│   ─ skip if GRIB already present and valid
│
└── Phase 3: Transform (per month, parallel or sequential)
    transform.py × 12 months
    ─ open GRIBs with cfgrib (dask-backed)
    ─ apply_derived_fields: GHI, DHI, DNI, T, WS_10M …
    ─ export.py → monthly NC (zlib complevel=1 float32)
    ─ cleanup: delete GRIB files after successful write
```
