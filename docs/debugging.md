# COSMO-REA6 Pipeline — Debugging Guide

Common errors encountered when running the pipeline, with causes and fixes.

---

## 1. Missing Python package: `ModuleNotFoundError`

### `ModuleNotFoundError: No module named 'cfgrib'` (or eccodes, xarray, dask …)

**Cause:** The weather conda environment is not activated, or was created
without all dependencies.

**Fix:**

```bash
conda activate weather_env

# If the environment does not exist yet:
conda env create -f infrastructure/env/weather_env.yml
conda activate weather_env
pip install -e . --no-deps
```

### `ModuleNotFoundError: No module named 'pvlib'`

**Cause:** pvlib is an optional dependency used only for MERRA-2 and
ERA5-Land DIRINT decomposition.  It is listed in `weather_env.yml` but
not in the core `pyproject.toml` dependencies.

**Fix:**

```bash
conda install -n weather_env -c conda-forge "pvlib=0.11.*"
# or
pip install "weather[solar]"
```

---

## 2. Missing system packages (conda-only tools)

### `lbzip2` / `pbzip2` not found — pipeline falls back to Python bz2

**Symptom:** Decompression is slower than expected; log shows
`"decompressor: python-bz2"` instead of `"decompressor: lbzip2"`.

**Cause:** `lbzip2` and `pbzip2` are compiled C binaries distributed via
conda-forge.  They are **not available via pip/PyPI**.  If the conda
environment was created without them (or the package was not found on the
target platform), `common/decompress.py` automatically falls back to
Python's `bz2` stdlib module — which is always single-threaded, regardless
of `COSMO_THREADS_PER_JOB`.

**Fix:**

```bash
conda install -n weather_env -c conda-forge lbzip2
# Verify:
which lbzip2          # Linux/macOS
lbzip2 --version
```

If `lbzip2` is unavailable on your HPC (e.g., RHEL-based clusters), try
`pbzip2`:

```bash
conda install -n weather_env -c conda-forge pbzip2
```

**Note on Windows:** Neither `lbzip2` nor `pbzip2` has a native Windows
conda-forge build.  On Windows the pipeline always uses Python bz2 fallback.
This is fine for development and testing; use Linux/HPC for production runs.

---

## 3. HDF5 / NetCDF file-locking errors on HPC

### `OSError: Unable to open file (unable to lock file, errno = 11, ...)`

> Full message: `unable to lock file, errno = 11, error message = 'Resource temporarily unavailable'`

**Cause:** GPFS and Lustre network file systems (common on HPC clusters) do
not support POSIX advisory file locks.  HDF5 (used by NetCDF4 and h5py)
requests a file lock on open, which fails or hangs on these file systems.

**Fix:** Set the environment variable before running:

```bash
export HDF5_USE_FILE_LOCKING=FALSE
```

This is already set automatically in:

- `test_cosmo_one_year.py` (`os.environ.setdefault(...)` at module level)
- `test_percentile.py` (same)
- `infrastructure/container/Dockerfile` (`ENV HDF5_USE_FILE_LOCKING=FALSE`)

If you run scripts in a way that bypasses these files (e.g., importing
modules directly), add the export to your SLURM job script or `.bashrc`.

---

## 4. `NetCDF: Not a valid ID` during merge or close

**Symptom:** Error traceback ending in
`RuntimeError: NetCDF: Not a valid ID` from `netCDF4._netCDF4.Dataset.close`.

**Cause:** This is a known bug in `netCDF4-python` (the Python bindings for
the C library) on GPFS/NFS when a dataset object is closed after the
underlying file descriptor has been invalidated by a network interruption or
a fork().  It typically occurs when `xr.open_mfdataset` is used to merge
many large files.

**Update (2026-08-12):** `weather.common.merge.NetCDFMerger` was rewritten
from hand-rolled h5py to `xr.open_mfdataset`/`to_netcdf` (fixing two
separate, more severe bugs in the old implementation — missing HDF5
dimension scales and silently wrong cross-file timestamps; see
`docs/WEATHER_FETCH_GUIDE.md`'s history notes). This means the advice
below no longer applies as written: `NetCDFMerger` now uses
`xr.open_mfdataset` internally, so it no longer sidesteps this bug by
construction the way the old h5py implementation did.

What's actually known: a real, full-scale production merge with the new
implementation (112 GB, 12 monthly COSMO-REA6 files, ~5.4 hours, real
GPFS/NFS-mounted storage) completed with zero `NetCDF: Not a valid ID`
errors. That's evidence the new implementation doesn't trip this bug in
practice, not proof the underlying risk is gone — the specific trigger
condition (fork() or a network interruption invalidating a file
descriptor mid-run) wasn't deliberately reproduced. If you hit this
error with the current `NetCDFMerger`, the still-relevant mitigation is
`HDF5_USE_FILE_LOCKING=FALSE` (see item 3 above) plus retrying the
merge — there is currently no h5py-based fallback path to reach for.

---

## 5. Download errors

### `ConnectionError` / `requests.exceptions.ReadTimeout`

**Cause:** DWD OpenData server is temporarily unavailable, or the HPC network
has rate limiting or firewall rules blocking outbound HTTPS to DWD.

**Fix:**

```bash
# Re-run with --skip-decompress --resume to retry only failed downloads:
python src/weather/tests/test_cosmo_one_year.py \
    --year 2018 --ncores 94 --resume
```

The download function uses atomic rename — a partial download leaves no
corrupt file, so re-running is always safe.

### `RuntimeError: Empty file after download: <path>.grb.bz2`

**Cause:** DWD returned a 0-byte response (server error or network drop).

**Fix:** Delete the empty file and re-run:

```bash
find /data/download -name "*.grb.bz2" -size 0 -delete
python src/weather/tests/test_cosmo_one_year.py --year 2018 --ncores 94 --resume
```

---

## 6. Decompression errors

### `FileNotFoundError: Compressed file not found: <path>.grb.bz2`

**Cause:** Phase 1 (download) failed for this attribute/month, or the bz2
file was already cleaned up but `--skip-download` is set and `--skip-decompress`
is also set, bypassing Phase 2 entirely.

**Fix:** Re-run without `--skip-download` for the affected year/month, or
check the log for the download failure.

### Decompression verification FAILED: `Not expanded` / `Bad GRIB header`

**Cause:** The bz2 file was corrupted during download (partial transfer,
checksum mismatch).

**Fix:**

```bash
# Delete the bad bz2 and grb files, then re-run:
rm /data/download/T_2M/T_2M.2D.201801.grb.bz2
rm /data/decompress/T_2M/T_2M.2D.201801.grb
python src/weather/tests/test_cosmo_one_year.py --year 2018 --ncores 94 --resume
```

---

## 7. Memory errors during transform

### `MemoryError` or process killed (SIGKILL) during Phase 3

**Cause:** Peak RAM per month is approximately `ncores × 0.5 GB`.  At 94
workers this is ~47 GB.  If the node has less RAM, or is shared with other
jobs, the OOM killer terminates the process.

**Fix options:**

1. Reduce `--ncores`:

   ```bash
   python src/weather/tests/test_cosmo_one_year.py --year 2018 --ncores 48
   ```

2. Add a SLURM memory limit that reserves enough headroom:

   ```bash
   #SBATCH --mem=60G
   #SBATCH --cpus-per-task=94
   ```

3. For a shared node, use `--ncores $(( $(nproc) / 2 ))`.

See [parallelization.md §6](parallelization.md#6-memory-footprint) for the
full memory formula.

---

## 8. SLURM job killed mid-run

**Symptom:** Job ends with exit code 137 (SIGKILL) or the wall-time limit
is reached.

**Fix:** Use `--resume` to continue from where the job stopped:

```bash
python src/weather/tests/test_cosmo_multi_year.py \
    --from-year 1995 --to-year 2027 \
    --ncores 94 --resume
```

All completed monthly `.nc` files are detected automatically; only
incomplete years are reprocessed.

---

## 9. conda env update FutureWarning

**Symptom:**

```text
FutureWarning: `remote_definition` is deprecated and will be removed in 25.9.
```

**Cause:** Your conda version (≥ 25.x) issues a deprecation warning for the
`--file` syntax in `conda update`.  This is a warning only — it does not
affect the update.

**Fix:** Use the correct command (`conda env update`, not `conda update`):

```bash
conda env update -n weather_env -f infrastructure/env/weather_env.yml --prune
```

---

## 10. `PackageNotInstalledError` from conda

**Symptom:**

```text
PackageNotInstalledError: Package is not installed in prefix.
  package name: ./infrastructure/env/weather_env.yml
```

**Cause:** `conda update` was used with a YAML file path as the package name.
`conda update` is for individual package names; `conda env update` is for
YAML environment files.

**Fix:** Use the correct command:

```bash
conda env update -n weather_env -f infrastructure/env/weather_env.yml --prune
```

---

## 11. Docker: `head` not found (PowerShell)

**Symptom:** Commands like `docker run ... 2>&1 | head -20` fail in
PowerShell with `CommandNotFoundException`.

**Cause:** `head` is a Unix command not available in PowerShell.

**Fix:** Use PowerShell's equivalent:

```powershell
docker run ... 2>&1 | Select-Object -First 20
```

---

## 12. `DatasetBuildError: multiple values for unique key` (cfgrib / GRIB)

**Symptom:**

```text
cfgrib.dataset.DatasetBuildError: multiple values for unique key,
try re-open the file with one of:
    filter_by_keys={'uvRelativeToGrid': 0}
    filter_by_keys={'uvRelativeToGrid': 1}
```

**Cause:** Certain months in the DWD COSMO-REA6 archive contain GRIB
messages with both `uvRelativeToGrid=0` (scalar / earth-relative) and
`uvRelativeToGrid=1` (grid-relative wind) in the same file.  This is
valid GRIB2 but cfgrib cannot disambiguate without a filter.

Known affected months (confirmed): **2005-02**, **2012-12**.
Other months may be affected too — the robust opener handles all cases
automatically.

**Fix (already applied in `transform.py`):**

`open_grib_month()` now calls `_open_grib_robust()`, which:

1. Tries `xr.open_dataset()` without any filter (succeeds for all normal
   months).
2. If cfgrib raises `DatasetBuildError` containing
   `"multiple values for unique key"`, retries with
   `filter_by_keys={'uvRelativeToGrid': 0}` then `=1`, returning the
   first response that yields a non-empty dataset.

No manual intervention is needed; the pipeline handles this transparently.

**If you see an empty-dataset error instead** (`KeyError: "No data
variables in dataset"`), it means a hard-coded filter was applied and
returned zero messages — ensure you are running the latest `transform.py`
where the robust opener replaces any static `filter_by_keys`.

**The `.idx` files next to `.grb` files are normal:** cfgrib writes an
ECCODES index cache file (`.grb.idx`) alongside every GRIB file on first
open.  These files speed up subsequent reads and are harmless.

## 13. `weather geo crop` / `cdo sellonlatbox` aborts: `Unsupported grid type: generic`

**Symptom:**

```text
cdi    warning (set_coordinates_varids): Coordinates variable latitude can't be assigned!
cdi    warning (set_coordinates_varids): Coordinates variable longitude can't be assigned!
cdo    sellonlatbox (Warning): Unsupported grid type: generic
cdo    sellonlatbox (Abort): No processable variable found!
```

**Cause:** The output file's `latitude`/`longitude` coordinate variables
are missing CF `standard_name`/`units` attributes (only `_FillValue` is
set) — an `xarray.Dataset.assign_coords()` artifact in each provider's
`transform.py`, present in every export before this was fixed. CDO can't
identify the coordinates without them and falls back to a grid type with
no geographic meaning at all.

**Fix (already applied for new exports):** `transform.py` now calls
`common.cf_conventions.attach_cf_latlon_attrs()` right after assigning
`latitude`/`longitude`, for all three providers.

**If you hit this against an already-exported file**, it predates the
fix and needs the same attributes patched in (metadata-only, no data
recomputation — see the full writeup for the exact approach and the
retroactive pass already run against the COSMO-REA6 and MERRA-2
archives): [cdo_crop_cf_metadata.md](cdo_crop_cf_metadata.md).
