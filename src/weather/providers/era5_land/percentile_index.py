"""ERA5-Land percentile indexer - pure CPU/numpy implementation.

Mirrors ``cosmo_rea6.percentile_index`` (the selection method
actually in production there; the older ``BasePercentileAnalyzer``/
``percentile.py`` approach was superseded and removed). Derives P10,
P50, and P90 representative-year mosaics for every grid cell across the
ERA5-Land Europe-crop grid by ranking candidate years on cumulative
monthly GHI, per ``docs/percentile_methodology.md``.

Pipeline
--------
1. Load   : monthly NetCDF files (``ERA5_LAND_<YYYY>_<MM>_all_attrs.nc``)
            read in parallel. Daily GHI sums extracted; leap days removed.
2. Select : For each month and cell, total each year's daily GHI and
            pick the year nearest the 10th/50th/90th percentile of
            those totals taken across years. Pure numpy.
3. Mosaic : Spawn workers write 36 output NetCDF files (12 months x
            3 percentiles). Each worker opens one source file at a
            time and copies all variables for winning cells.

Differences from COSMO
-----------------------
- Grid size (``n_y``/``n_lon``) is inferred from the first loaded file
  rather than hardcoded (COSMO's 824x848 does not apply to the ERA5-Land
  Europe crop; the exact shape depends on ``ERA5_AREA``).
- Source/target directories default to ``EnvSettings``-resolved paths
  (``era5_output_dir``), not hardcoded ``/data/soma`` paths.
- Filename pattern is ``ERA5_LAND_<YYYY>_<MM>_all_attrs.nc``.
- Boundary repair MUST have run first (now automatic — see
  ``boundary_repair.py`` and ``pipeline.py``'s STEP 3/3) — an unrepaired
  first stamp would corrupt the January daily GHI sum.

Run with ``--clean`` to remove existing output before re-running.
Existing valid output files are skipped automatically.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import logging
import os
import re
import shutil
import tempfile
import warnings
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import (
    ProcessPoolExecutor,
    as_completed,
)

import numpy as np
import xarray as xr

# Disable HDF5 file locking globally.  On NFS/GPFS file
# systems, .lock files left by a killed process cause the
# next open_dataset call to hang indefinitely.
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Top-level worker functions
#
# These MUST be defined at module top level (not nested inside a class or
# another function) so that ProcessPoolExecutor with the ``spawn`` start
# method can locate and import them in child processes.  Nesting them
# would make them unpicklable and cause immediate BrokenProcessPool errors.
# ---------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"ERA5_LAND_(\d{4})_(\d{2})_")


#: Written into ``source_year`` for cells that have no source data in
#: at least one year -- e.g. every ocean cell under ERA5-Land's static
#: land-sea mask, which is ~49% of that grid. Such cells are identical
#: in every candidate year, so any "winner" would be an artefact of
#: sort order rather than a real selection.
NO_SOURCE_YEAR = -1


def _month_hour_offset(times) -> int:
    """Hours from the start of the calendar month to ``times[0]``.

    Normally 0.  ERA5-Land's very first archive month (1950-01) is the
    exception: its 00:00 stamp does not exist upstream, so that file
    holds 743 hours starting at 01:00 while every other January holds
    744.  Callers use this to write a short year into the correct slots
    of a full-length mosaic instead of shifting it an hour early.

    Parameters
    ----------
    times : array-like of numpy.datetime64
        Time coordinate values, already leap-day filtered.

    Returns
    -------
    int
        Whole hours between the 1st of the month at 00:00 and
        ``times[0]``.
    """
    t0 = np.asarray(times)[0].astype("datetime64[h]")
    month_start = t0.astype("datetime64[M]").astype("datetime64[h]")
    return int((t0 - month_start) / np.timedelta64(1, "h"))


def _preprocess_single_file(file_path: str) -> dict:
    """Load one NetCDF file and return day-summed GHI without leap days.

    Parameters
    ----------
    file_path : str
        Absolute path to an ERA5-Land NetCDF file.  The filename must
        contain an ``ERA5_LAND_YYYY_MM_`` segment used to extract year
        and month.

    Returns
    -------
    dict
        On success: keys ``path``, ``year``, ``month``, ``data``
        (numpy array of daily GHI sums, shape ``(days, y, x)``),
        and ``success=True``.
        On failure: keys ``path``, ``success=False``, ``error`` (str).
    """
    try:
        filename = os.path.basename(file_path)
        match = _FILENAME_RE.search(filename)
        if match is None:
            raise ValueError(
                f"Filename does not match expected pattern: {filename}"
            )
        year = int(match.group(1))
        month = int(match.group(2))

        with xr.open_dataset(file_path, engine="netcdf4") as ds:
            if "GHI" not in ds.data_vars:
                raise KeyError(f"Missing 'GHI' variable in {filename}")
            leap_mask = (
                (ds.time.dt.month == 2) & (ds.time.dt.day == 29)
            )
            # Keep only stamps belonging to this file's OWN calendar
            # month. COSMO-REA6's monthly exports carry a trailing
            # stamp from the next month, which resampled a 28-day
            # February into 29 daily bins (30 in leap years, the last
            # all-NaN). That both adds a spurious partial day to the
            # year's total and leaves years with differing bin counts
            # incomparable. No-op for ERA5-Land and MERRA-2, whose
            # exports already stop at the month boundary.
            in_month = ds.time.dt.month == month
            ghi = ds["GHI"].isel(time=in_month & ~leap_mask).load()

        # min_count=1 keeps a day NaN when every hour of it is NaN.
        # Without it a masked cell (ERA5-Land's ocean is NaN at every
        # hour of every year) sums to 0.0 and becomes indistinguishable
        # from a real polar-night day that genuinely received no sun.
        daily_sum = (
            ghi.resample(time="1D")
            .sum(dim="time", min_count=1)
            .values
        )
        return {
            "path": file_path,
            "year": year,
            "month": month,
            "data": daily_sum,
            "shape": daily_sum.shape[1:],
            "success": True,
        }
    except Exception as exc:
        return {
            "path": file_path,
            "success": False,
            "error": str(exc),
        }


def _build_month_mosaic(args: tuple) -> str:
    """Build and write P10/P50/P90 NetCDF mosaics for one month.

    Design
    ------
    The KS phase identifies the best source year per grid cell using
    GHI only.  The mosaic phase then copies **all variables** from that
    winning year into the output — GHI is not special here, every
    variable in the source file is carried across.

    The key insight that makes this efficient: once we know which year
    wins each cell for P10/P50/P90, we open **each source file exactly
    once** and write every variable and all three percentiles in a
    single pass.  We never loop over variables as an outer loop, and
    we never re-open the same file more than once.

    Outer loop  : years (one file open per iteration)
    Inner work  : read all variables at once, scatter-write winning
                  cells into all three percentile mosaics simultaneously

    This is safe to call from a spawned worker process because this
    module imports no CUDA/GPU libraries at module level.

    Parameters
    ----------
    args : tuple
        A four-element tuple ``(month_idx, spatial_index_maps,
        file_path_lookup, target_dir)`` where:

        - ``month_idx`` (int): month number 1-12.
        - ``spatial_index_maps`` (dict): maps keys like ``"P10_01"``
          to ``(y, x)`` int32 arrays of best source years.
        - ``file_path_lookup`` (dict): maps ``(year, month)`` tuples
          to NetCDF file paths.
        - ``target_dir`` (str): directory to write output files.

    Returns
    -------
    str
        Status message, e.g. ``"Month 01: OK"`` or a skip reason.
    """
    (
        month_idx,
        spatial_index_maps,
        file_path_lookup,
        target_dir,
    ) = args

    log = logging.getLogger(__name__)
    month_str = f"{month_idx:02d}"

    # Ensure HDF5 locking is disabled inside the spawn worker too.
    # The env var set at module level is not always inherited.
    os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

    # ── Clean up stale HDF5 lock files ─────────────────────────
    # HDF5 creates <file>.nc.lock when a writer opens a file and
    # removes it on clean close.  Ctrl+C or SIGKILL leaves them
    # behind, causing the next open_dataset to block forever.
    for _pct in ("p10", "p50", "p90"):
        _lock = os.path.join(
            target_dir,
            f"era5_land_{_pct}_{month_str}_all_attrs.nc.lock",
        )
        if os.path.exists(_lock):
            log.warning(
                "Month %s: removing stale lock file %s",
                month_str, os.path.basename(_lock),
            )
            try:
                os.remove(_lock)
            except OSError as _le:
                log.warning(
                    "Month %s: could not remove lock: %s",
                    month_str, _le,
                )

    # ── Skip if all output files exist and are valid ───────────
    # A file < 1 MB is almost certainly a partial write from an
    # interrupted run.  Remove it and regenerate.
    _min_bytes = 1 * 1024 * 1024
    expected_files = [
        os.path.join(
            target_dir,
            f"era5_land_{pct.lower()}_{month_str}_all_attrs.nc",
        )
        for pct in ("P10", "P50", "P90")
    ]
    if all(os.path.exists(f) for f in expected_files):
        small = [
            f for f in expected_files
            if os.path.getsize(f) < _min_bytes
        ]
        if small:
            log.warning(
                "Month %s: partial write detected (%d file(s)"
                " < 1 MB) — removing and regenerating",
                month_str, len(small),
            )
            for f in small:
                os.remove(f)
        else:
            try:
                for f in expected_files:
                    with xr.open_dataset(f, engine="netcdf4") as _chk:
                        _ = _chk.sizes
                log.info(
                    "Month %s: all output files valid, skipping",
                    month_str,
                )
                return (
                    f"Month {month_str}: skipped (already done)"
                )
            except Exception as _e:
                log.warning(
                    "Month %s: output files corrupt (%s)"
                    " — regenerating",
                    month_str, _e,
                )
                for f in expected_files:
                    if os.path.exists(f):
                        os.remove(f)

    # All unique years that appear as winners in any percentile map
    sorted_years: list[int] = sorted({
        int(y)
        for pct in ("P10", "P50", "P90")
        for key in (f"{pct}_{month_str}",)
        if key in spatial_index_maps
        for y in np.unique(spatial_index_maps[key])
        if int(y) != NO_SOURCE_YEAR
    })
    if not sorted_years:
        return f"Month {month_str}: skipped (no spatial maps)"

    year_to_idx: dict[int, int] = {
        y: i for i, y in enumerate(sorted_years)
    }

    # Per-percentile (R, L) arrays: local year index that wins each cell
    pct_y_idx: dict[str, np.ndarray] = {
        # -1 for the no-source sentinel: it matches no real year
        # index below, so those cells are simply never written and stay
        # NaN in the mosaic.
        pct: np.vectorize(
            lambda _v: year_to_idx.get(int(_v), -1)
        )(
            spatial_index_maps[f"{pct}_{month_str}"]
        ).astype(np.intp)
        for pct in ("P10", "P50", "P90")
    }

    # Pre-compute winning cell coordinates per (percentile, year) so
    # the inner loop does no repeated boolean operations.
    # pct_winners[(pct, local_yi)] = (row_indices, col_indices)
    pct_winners: dict[tuple, tuple] = {}
    for pct in ("P10", "P50", "P90"):
        for local_yi in range(len(sorted_years)):
            wr, wl = np.where(pct_y_idx[pct] == local_yi)
            if wr.size > 0:
                pct_winners[(pct, local_yi)] = (wr, wl)

    # Time axes are not identical across years.  ERA5-Land's first
    # archive month (1950-01) is missing its 00:00 stamp, so it carries
    # 743 hours starting at 01:00 while every other January carries
    # 744.  Sizing the mosaic from an arbitrary year -- previously the
    # earliest winning year -- leaves the array an hour short and the
    # next year then fails to broadcast into it.  Measure every winning
    # year first: take the longest axis as the canonical month length
    # and remember each year's offset.
    year_offsets: dict[int, int] = {}
    year_lengths: dict[int, int] = {}
    for _year in sorted_years:
        _fp = file_path_lookup.get((_year, month_idx))
        if not _fp or not os.path.exists(_fp):
            continue
        with xr.open_dataset(_fp, engine="netcdf4") as _tds:
            # Same clip the daily sums use (see
            # _preprocess_single_file): drop leap days AND any stamp
            # belonging to a neighbouring month. Without the second
            # half the mosaic is sized one hour longer than the data
            # it holds, and to_netcdf rejects the mismatch with
            # "conflicting sizes for dimension 'time'".
            _tkeep = (_tds.time.dt.month == month_idx) & ~(
                (_tds.time.dt.month == 2) & (_tds.time.dt.day == 29)
            )
            _tvals = _tds.isel(time=_tkeep).time.values
        year_offsets[_year] = _month_hour_offset(_tvals)
        year_lengths[_year] = int(len(_tvals))

    if not year_lengths:
        return f"Month {month_str}: failed (no readable source files)"

    # Offsets are relative to the EARLIEST start among the winning
    # years, not to midnight. COSMO-REA6 stamps hours as ending
    # (01:00 .. next month 00:00), so every one of its years starts at
    # hour 1; anchoring on midnight would size the mosaic one slot
    # longer than any file can fill, and since the output time
    # coordinate is copied from a real file it would then be one stamp
    # short of the data ("conflicting sizes for dimension 'time'").
    _base = min(year_offsets.values())
    for _y in year_offsets:
        year_offsets[_y] -= _base
    t_size = max(
        year_offsets[_y] + year_lengths[_y] for _y in year_lengths
    )

    # Prefer a reference year whose axis spans the whole month from
    # hour 0 -- its time coordinate is copied verbatim into the output.
    ref_candidates = sorted(
        year_lengths,
        key=lambda _y: (
            year_offsets[_y] == 0 and year_lengths[_y] == t_size,
            year_lengths[_y],
            -_y,
        ),
        reverse=True,
    )

    # Scan those years to find a reference file that contains 3-D
    # (time, y, x) variables.
    spatial_dims = {"y", "x"}
    ref_fp: str | None = None
    var_names: list[str] = []
    var_dims: dict[str, tuple] = {}
    var_attrs: dict[str, dict] = {}
    ref_coords = None
    ref_attrs: dict = {}

    for year in ref_candidates:
        fp = file_path_lookup.get((year, month_idx))
        if not fp or not os.path.exists(fp):
            continue
        with xr.open_dataset(fp, engine="netcdf4") as _ref_ds:
            keep = (_ref_ds.time.dt.month == month_idx) & ~(
                (_ref_ds.time.dt.month == 2)
                & (_ref_ds.time.dt.day == 29)
            )
            _ref_filt = _ref_ds.isel(time=keep)
            _candidates: list[str] = [
                str(v) for v in _ref_filt.data_vars
                if (
                    set(_ref_filt[v].dims) >= spatial_dims
                    and "time" in _ref_filt[v].dims
                    and _ref_filt[v].ndim == 3
                )
            ]
            if not _candidates:
                log.warning(
                    "Month %s year %d: no 3-D variables in %s,"
                    " trying next year",
                    month_str, year, fp,
                )
                continue
            # Found a usable reference file
            ref_fp = fp
            var_names = _candidates
            var_dims = {
                v: _ref_filt[v].dims for v in var_names
            }
            var_attrs = {
                v: dict(_ref_filt[v].attrs) for v in var_names
            }
            # Materialise coords before the dataset closes. Capture
            # (dims, values) uniformly rather than bare values -- see
            # cosmo_rea6/percentile_index.py's identical fix: harmless
            # here since ERA5-Land's lat/lon are 1-D dimension
            # coordinates, but xr.Dataset(coords={...}) cannot infer
            # dimension names from a bare N-D array in general, so this
            # is the correct form regardless of grid shape.
            ref_coords = {
                k: (_ref_filt.coords[k].dims, _ref_filt.coords[k].values)
                for k in _ref_filt.coords
            }
            ref_attrs = dict(_ref_filt.attrs)
        break  # stop after first usable year

    if ref_fp is None or not var_names:
        return (
            f"Month {month_str}: failed"
            f" (no year had 3-D variables)"
        )

    sample_map = spatial_index_maps[f"P10_{month_str}"]
    n_y, n_x = sample_map.shape

    # Output mosaics: {pct: {var: (T, R, L) float32 array}}
    # Initialised to NaN; only winning cells are written.
    mosaics: dict[str, dict[str, np.ndarray]] = {
        pct: {
            v: np.full(
                (t_size, n_y, n_x),
                np.nan,
                dtype=np.float32,
            )
            for v in var_names
        }
        for pct in ("P10", "P50", "P90")
    }

    # ── Main loop: one file open per year ───────────────────────────
    for local_yi, year in enumerate(sorted_years):
        # Check whether this year wins any cell in any percentile
        has_winners = any(
            (pct, local_yi) in pct_winners
            for pct in ("P10", "P50", "P90")
        )
        if not has_winners:
            continue

        fp = file_path_lookup.get((year, month_idx))
        if fp is None or not os.path.exists(fp):
            log.warning(
                "Month %s year %d: file not found, skipping",
                month_str, year,
            )
            continue

        # Open file once; read all variables in one pass
        with xr.open_dataset(fp, engine="netcdf4") as ds:
            keep = (ds.time.dt.month == month_idx) & ~(
                (ds.time.dt.month == 2) & (ds.time.dt.day == 29)
            )
            ds_filt = ds.isel(time=keep)
            year_vars: dict[str, np.ndarray] = {
                v: ds_filt[v].values.astype(np.float32)
                for v in var_names
                if v in ds_filt.data_vars
                and ds_filt[v].ndim == 3
            }

        actual_t = next(iter(year_vars.values())).shape[0]
        offset = year_offsets.get(year, 0)

        # Write winning cells for every percentile simultaneously
        for pct in ("P10", "P50", "P90"):
            key = (pct, local_yi)
            if key not in pct_winners:
                continue
            wr, wl = pct_winners[key]
            for v, year_data in year_vars.items():
                mosaics[pct][v][
                    offset:offset + actual_t, wr, wl
                ] = year_data[:actual_t, wr, wl]

        del year_vars

    # ── Write output files ──────────────────────────────────────────
    # Write to a local temp directory first to avoid NFS locking, then
    # move to the (possibly NFS) target atomically.
    os.makedirs(target_dir, exist_ok=True)
    saved: list[str] = []

    tmp_root = tempfile.mkdtemp(prefix=f"era5_land_m{month_str}_")
    try:
        for pct in ("P10", "P50", "P90"):
            map_key = f"{pct}_{month_str}"
            out_ds = xr.Dataset(coords=ref_coords, attrs=ref_attrs)
            for v in var_names:
                out_ds[v] = (
                    var_dims[v], mosaics[pct][v], var_attrs[v]
                )
            out_ds["source_year"] = (
                ["y", "x"],
                spatial_index_maps[map_key].astype(np.int32),
                {
                    "description": (
                        "Source year for this percentile; "
                        f"{NO_SOURCE_YEAR} where the cell has no "
                        "source data in at least one year"
                    )
                },
            )
            enc = {
                vn: {
                    "zlib": True,
                    "complevel": 1,
                    "dtype": (
                        "int32" if vn == "source_year"
                        else "float32"
                    ),
                }
                for vn in out_ds.data_vars
            }
            fname = (
                f"era5_land_{pct.lower()}"
                f"_{month_str}_all_attrs.nc"
            )
            tmp_path = os.path.join(tmp_root, fname)
            out_ds.to_netcdf(
                tmp_path,
                encoding=enc,
                engine="netcdf4",
                format="NETCDF4",
            )
            out_ds.close()
            final_path = os.path.join(target_dir, fname)
            shutil.move(tmp_path, final_path)
            saved.append(fname)
    finally:
        # Always remove the local temp dir, even on error
        shutil.rmtree(tmp_root, ignore_errors=True)

    log.info(
        "Month %s done: %s", month_str, ", ".join(saved)
    )
    return f"Month {month_str}: OK"


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

class Era5LandPercentileIndexer:
    """Create monthly P10/P50/P90 mosaics from historical ERA5-Land data.

    The pipeline has three sequential phases:

    1. **Load** (``compile_historical_baselines``): read all input
       NetCDF files (``ERA5_LAND_<YYYY>_<MM>_all_attrs.nc``) in parallel
       using up to ``n_cpu_cores`` workers, extract day-summed GHI, and
       organise results by month and year.

    2. **KS match** (``_compute_ks_for_month``): for each of the 12
       months, find the year whose empirical GHI distribution best
       matches the pooled P10, P50, and P90 thresholds via minimum
       Kolmogorov-Smirnov distance.  Runs in pure vectorised numpy.

    3. **Mosaic** (``construct_and_save_mosaics``): for each month and
       percentile, assemble the best-year hourly data into a spatial
       mosaic and write it to a compressed NetCDF file.  Uses spawned
       worker processes to parallelise across months.

    Straggler-hiding optimisation: the load phase fires the KS callback
    for each month as soon as its last file arrives, so KS work for
    completed months overlaps with the tail of the I/O phase rather
    than waiting for all files.

    Parameters
    ----------
    source_dir : str
        Directory containing input ERA5-Land NetCDF files.  Files must
        match the pattern ``ERA5_LAND_YYYY_MM_*.nc``.
    target_dir : str
        Directory where output percentile NetCDF files are written.
    n_cpu_cores : int, optional
        Number of parallel workers for the file-loading phase.  ERA5-Land
        processing is I/O-bound (unlike COSMO's CPU-bound bz2 decompress),
        so this defaults to 6 to match ``ERA5_NCORES`` rather than COSMO's
        94-core CPU-bound default.
    n_y, n_lon : int, optional
        Grid dimensions.  If ``None`` (default), inferred from the first
        successfully loaded source file — the ERA5-Land Europe-crop grid
        size depends on ``ERA5_AREA`` and should not be hardcoded.
    n_mosaic_workers : int, optional
        Number of parallel workers for the mosaic-writing phase.
        Default: 2.
    """

    def __init__(
        self,
        source_dir: str,
        target_dir: str,
        n_cpu_cores: int = 6,
        n_y: int | None = None,
        n_lon: int | None = None,
        n_mosaic_workers: int = 2,
    ) -> None:
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.n_cpu_cores = n_cpu_cores
        self.n_y = n_y
        self.n_lon = n_lon
        self.n_mosaic_workers = n_mosaic_workers

    # ------------------------------------------------------------------
    # Phase 1: parallel file loading
    # ------------------------------------------------------------------

    def compile_historical_baselines(
        self,
        file_paths: list,
        files_per_month: dict | None = None,
        on_month_ready: Callable | None = None,
    ) -> tuple:
        """Load all NetCDF files in parallel and group by month and year.

        When ``files_per_month`` and ``on_month_ready`` are both
        supplied, the callback is invoked on the main thread the moment
        every file for a given month has been loaded.  This lets KS
        work begin on finished months while the worker pool is still
        processing the remaining (straggler) files.

        Parameters
        ----------
        file_paths : list of str
            Sorted list of absolute paths to input NetCDF files.
        files_per_month : dict, optional
            Mapping ``{month: expected_file_count}`` pre-computed from
            filenames.  Required to trigger ``on_month_ready``.
        on_month_ready : callable, optional
            Function with signature
            ``(month: int, registry: dict, lookup: dict) -> None``
            called as soon as all files for ``month`` are loaded.

        Returns
        -------
        tuple
            A pair ``(monthly_registry, file_path_lookup)`` where:

            - ``monthly_registry`` is
              ``dict[int, dict[int, np.ndarray]]`` mapping
              ``month -> year -> daily_GHI_array``.
            - ``file_path_lookup`` is ``dict[(year, month), str]``
              mapping year/month pairs to their source file paths.
        """
        total = len(file_paths)
        logger.info(
            "Loading %d files with %d workers",
            total,
            self.n_cpu_cores,
        )

        monthly_registry: dict = {m: {} for m in range(1, 13)}
        file_path_lookup: dict = {}
        month_counts: dict = defaultdict(int)

        with ProcessPoolExecutor(
            max_workers=self.n_cpu_cores
        ) as pool:
            futures = {
                pool.submit(_preprocess_single_file, p): p
                for p in file_paths
            }

            for done_idx, fut in enumerate(
                as_completed(futures), 1
            ):
                try:
                    result = fut.result()
                except Exception as exc:
                    logger.error(
                        "Worker exception for %s: %s",
                        futures[fut],
                        exc,
                    )
                    if done_idx % 40 == 0 or done_idx == total:
                        logger.info(
                            "Progress: [%d/%d] (%.1f%%)",
                            done_idx,
                            total,
                            done_idx / total * 100,
                        )
                    continue

                if result["success"]:
                    month = result["month"]
                    year = result["year"]
                    monthly_registry[month][year] = result["data"]
                    file_path_lookup[(year, month)] = result["path"]
                    month_counts[month] += 1

                    if self.n_y is None or self.n_lon is None:
                        self.n_y, self.n_lon = result["shape"]

                    expected = (
                        files_per_month.get(month, -1)
                        if files_per_month is not None
                        else -1
                    )
                    if (
                        on_month_ready is not None
                        and month_counts[month] == expected
                    ):
                        logger.info(
                            "Month %02d ready - starting KS",
                            month,
                        )
                        on_month_ready(
                            month,
                            monthly_registry,
                            file_path_lookup,
                        )
                else:
                    logger.error(
                        "Failed: %s: %s",
                        result["path"],
                        result.get("error"),
                    )

                if done_idx % 40 == 0 or done_idx == total:
                    logger.info(
                        "Progress: [%d/%d] (%.1f%%)",
                        done_idx,
                        total,
                        done_idx / total * 100,
                    )

        return monthly_registry, file_path_lookup

    # ------------------------------------------------------------------
    # Phase 2: KS distribution matching
    # ------------------------------------------------------------------

    def _compute_ks_for_month(
        self,
        month: int,
        monthly_registry: dict,
    ) -> dict:
        """Run KS distribution matching for one month.

        For each grid cell, totals that year's daily GHI sums and
        selects the year sitting nearest the 10th, 50th and 90th
        percentile of those totals taken across all years.

        Parameters
        ----------
        month : int
            Month number 1-12.
        monthly_registry : dict
            Nested dict ``{month: {year: daily_GHI_array}}``.

        Returns
        -------
        dict
            Keys ``"P10_MM"``, ``"P50_MM"``, ``"P90_MM"`` (where
            ``MM`` is the zero-padded month) each mapping to a
            ``(y, x)`` int32 array of best-source-year values.
            Returns an empty dict if no data is available for the month.
        """
        available_years = sorted(monthly_registry[month].keys())
        n_years = len(available_years)
        if n_years == 0:
            return {}

        if self.n_y is None or self.n_lon is None:
            self.n_y, self.n_lon = (
                monthly_registry[month][available_years[0]].shape[1:]
            )

        max_days = max(
            monthly_registry[month][y].shape[0]
            for y in available_years
        )

        stacked = np.zeros(
            (n_years, max_days, self.n_y, self.n_lon),
            dtype=np.float32,
        )
        for yi, y in enumerate(available_years):
            n_days = monthly_registry[month][y].shape[0]
            stacked[yi, :n_days] = monthly_registry[month][y]

        # Per-year monthly GHI total for every cell: shape (Y, R, L).
        # This is the ranking metric documented in
        # docs/percentile_methodology.md section 2 -- ASHRAE TMY3 ranks
        # candidate years by cumulative monthly global radiation.
        #
        # It replaces a "fraction of this year's days at or below the
        # pooled P-threshold" statistic that could not deliver the
        # documented P-levels: a TYPICAL year has ~10% of its days
        # below the pooled P10 by definition, so argmin|cdf - 0.10|
        # picked the typical year and rejected genuinely cloudy ones,
        # and argmin|cdf - 0.90| likewise rejected sunny ones. Being a
        # count / max_days it also quantised to max_days + 1 levels, so
        # dozens of years tied in every cell and np.argmin handed each
        # tie to whichever year sorted first.
        year_total = stacked.sum(axis=1, dtype=np.float64)

        # Target radiation level per percentile, taken ACROSS years.
        # np.quantile interpolates between the two bracketing years;
        # argmin then snaps to whichever real year sits nearest, so the
        # chosen year's brightness rank comes out at ~= q.
        # A year with no data at a cell must not compete for it, but it
        # must not disqualify the cell either: one bad year previously
        # invalidated every cell of the month.
        finite = np.isfinite(year_total)

        best: dict[float, np.ndarray] = {}
        with warnings.catch_warnings():
            # All-NaN cells are expected (ocean); they are flagged
            # below rather than ranked.
            warnings.simplefilter("ignore", category=RuntimeWarning)
            for _q in (0.10, 0.50, 0.90):
                _target = np.nanquantile(year_total, _q, axis=0)
                _dist = np.where(
                    finite, np.abs(year_total - _target[None]), np.inf
                )
                best[_q] = np.argmin(_dist, axis=0).astype(np.int32)
                del _target, _dist

        # Flag only cells no year can speak for at all -- e.g. ocean
        # under ERA5-Land's static land-sea mask. Ranking those would
        # just hand them to whichever year sorts first.
        valid_cell = finite.any(axis=0)

        best_p10 = best[0.10]
        best_p50 = best[0.50]
        best_p90 = best[0.90]
        del year_total, best, finite

        year_arr = np.array(available_years, dtype=np.int32)
        tag = f"{month:02d}"

        selections: dict[str, np.ndarray] = {}
        for _name, _best in (
            ("P10", best_p10), ("P50", best_p50), ("P90", best_p90)
        ):
            _sel = year_arr[_best]
            _sel[~valid_cell] = NO_SOURCE_YEAR
            selections[f"{_name}_{tag}"] = _sel

        logger.info(
            "Month %s selection done (%d cell(s) without source data; "
            "unique years P10 %d, P50 %d, P90 %d)",
            tag,
            int((~valid_cell).sum()),
            *(
                len(np.unique(selections[f"{_p}_{tag}"][valid_cell]))
                for _p in ("P10", "P50", "P90")
            ),
        )
        return selections

    # ------------------------------------------------------------------
    # Phase 3: mosaic assembly
    # ------------------------------------------------------------------

    def construct_and_save_mosaics(
        self,
        spatial_index_maps: dict,
        file_path_lookup: dict,
    ) -> None:
        """Write 36 output NetCDF files (12 months x 3 percentiles).

        One job per month runs in a pool of ``n_mosaic_workers``
        spawned processes.  ``spawn`` gives each child a clean
        interpreter with no inherited file descriptors or shared state.

        This phase is I/O-bound: each month reads up to N source files.
        Keep ``n_mosaic_workers`` small (1-2) so the workers do not
        saturate NFS/network bandwidth and stall each other.  There is
        no per-job timeout; every month runs to completion.

        Output is written to local temp storage first, then moved to
        the target directory, avoiding NFS write locks and HDF5
        contention.

        Parameters
        ----------
        spatial_index_maps : dict
            Maps keys like ``"P10_01"`` to ``(y, x)`` int32 arrays
            of best-source-year values from ``_compute_ks_for_month``.
        file_path_lookup : dict
            Maps ``(year, month)`` tuples to source NetCDF file paths.
        """
        import multiprocessing

        logger.info("Assembling mosaics -> %s", self.target_dir)
        os.makedirs(self.target_dir, exist_ok=True)

        month_args = [
            (
                month_num,
                spatial_index_maps,
                file_path_lookup,
                self.target_dir,
            )
            for month_num in range(1, 13)
        ]

        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=self.n_mosaic_workers, mp_context=ctx
        ) as pool:
            futures = {
                pool.submit(_build_month_mosaic, a): a[0]
                for a in month_args
            }
            for fut in as_completed(futures):
                month_num = futures[fut]
                try:
                    logger.info(fut.result())
                except Exception as exc:
                    logger.error(
                        "Month %02d failed: %s "
                        "(run with --month %d to retry)",
                        month_num, exc, month_num,
                    )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def execute_indexing_pipeline(self) -> None:
        """Run all three pipeline phases with straggler-hiding overlap.

        Discovers input files, pre-counts files per month, then runs
        the load phase.  As each month's files complete, the KS phase
        starts immediately for that month rather than waiting for all
        files to finish (straggler hiding).  Finally runs the mosaic
        phase once all KS maps are ready.

        Raises
        ------
        FileNotFoundError
            If no ``.nc`` files are found in ``source_dir``.
        """
        file_paths = sorted(
            glob.glob(os.path.join(self.source_dir, "ERA5_LAND_*.nc"))
        )
        if not file_paths:
            raise FileNotFoundError(
                f"No ERA5_LAND_*.nc files found in {self.source_dir}"
            )

        # Check upfront whether all 36 output files exist and are
        # valid.  If so, skip the entire load + KS phases and go
        # straight to the mosaic phase, which will find each month
        # already done and return immediately.
        all_outputs = [
            os.path.join(
                self.target_dir,
                f"era5_land_{pct.lower()}_{m:02d}_all_attrs.nc",
            )
            for pct in ("P10", "P50", "P90")
            for m in range(1, 13)
        ]
        _min_bytes = 1 * 1024 * 1024  # 1 MB
        _existing = [f for f in all_outputs if os.path.exists(f)]
        _small = [
            f for f in _existing
            if os.path.getsize(f) < _min_bytes
        ]
        if _small:
            logger.warning(
                "%d output file(s) look like partial writes"
                " (< 1 MB) — will regenerate: %s",
                len(_small),
                [os.path.basename(f) for f in _small],
            )
            for f in _small:
                os.remove(f)
        elif len(_existing) == len(all_outputs):
            logger.info(
                "All 36 output files exist and appear valid."
                " Nothing to do."
            )
            return

        files_per_month: dict = defaultdict(int)
        for fp in file_paths:
            fname_match = _FILENAME_RE.search(os.path.basename(fp))
            if fname_match is None:
                continue
            files_per_month[int(fname_match.group(2))] += 1

        logger.info(
            "Found %d files; per-month counts: %s",
            len(file_paths),
            dict(sorted(files_per_month.items())),
        )

        spatial_index_maps: dict = {}
        processed_months: set = set()

        def _on_month_ready(
            month: int,
            monthly_registry: dict,
            _lookup: dict,
        ) -> None:
            """KS callback fired when a month's files are all loaded.

            Parameters
            ----------
            month : int
                Month number 1-12.
            monthly_registry : dict
                Current state of the registry (may still be growing
                for other months).
            _lookup : dict
                File-path lookup (unused here; KS only needs registry).
            """
            if month in processed_months:
                return
            processed_months.add(month)
            spatial_index_maps.update(
                self._compute_ks_for_month(month, monthly_registry)
            )

        monthly_registry, file_path_lookup = (
            self.compile_historical_baselines(
                file_paths,
                files_per_month=dict(files_per_month),
                on_month_ready=_on_month_ready,
            )
        )

        # Handle any months not processed via callback
        # (e.g. filename parse failures or count mismatches)
        remaining = [
            month_num
            for month_num in range(1, 13)
            if (
                month_num not in processed_months
                and monthly_registry[month_num]
            )
        ]
        if remaining:
            logger.info(
                "KS for remaining months: %s", remaining
            )
            for month_num in remaining:
                spatial_index_maps.update(
                    self._compute_ks_for_month(
                        month_num, monthly_registry
                    )
                )

        self.construct_and_save_mosaics(
            spatial_index_maps, file_path_lookup
        )
        logger.info("Pipeline complete - 36 output files written.")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from ...settings import EnvSettings

    _parser = argparse.ArgumentParser(
        description="ERA5-Land percentile indexer",
    )
    _parser.add_argument(
        "--source-dir",
        default=None,
        help=(
            "Directory containing monthly ERA5_LAND_*.nc files."
            " Default: EnvSettings.era5_output_dir()"
        ),
    )
    _parser.add_argument(
        "--target-dir",
        default=None,
        help=(
            "Directory to write percentile output files."
            " Default: <source-dir>/percentile"
        ),
    )
    _parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Remove all existing output files before running."
            " Use this to force a full re-run."
        ),
    )
    _parser.add_argument(
        "--month",
        type=int,
        action="append",
        dest="months",
        metavar="M",
        help=(
            "Force re-processing of month M (1-12), deleting"
            " any existing output for that month first."
            " Can be repeated: --month 2 --month 11"
        ),
    )
    _args = _parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s CET - %(levelname)s - %(message)s",
    )

    _log = logging.getLogger(__name__)
    _source = _args.source_dir or str(EnvSettings.era5_output_dir())
    _target = _args.target_dir or os.path.join(_source, "percentile")

    if _args.clean and os.path.exists(_target):
        _log.info(
            "--clean: removing existing output dir %s", _target
        )
        shutil.rmtree(_target)

    # --month: delete only the specified months' output files so
    # the skip-if-exists check forces them to be rebuilt.
    if _args.months:
        for _m in _args.months:
            if not 1 <= _m <= 12:
                _parser.error(
                    f"--month {_m} is out of range (1-12)"
                )
            for _pct in ("p10", "p50", "p90"):
                _f = os.path.join(
                    _target,
                    f"era5_land_{_pct}_{_m:02d}_all_attrs.nc",
                )
                if os.path.exists(_f):
                    os.remove(_f)
                    _log.info(
                        "--month %d: removed %s",
                        _m, os.path.basename(_f),
                    )

    # Clean stale HDF5 lock files from both source and output dirs.
    for _lock_dir in (_source, _target):
        if not os.path.exists(_lock_dir):
            continue
        _locks = [
            os.path.join(_lock_dir, f)
            for f in os.listdir(_lock_dir)
            if f.endswith(".lock")
        ]
        if _locks:
            _log.info(
                "Removing %d stale lock file(s) in %s",
                len(_locks), _lock_dir,
            )
            for _lf in _locks:
                with contextlib.suppress(OSError):
                    os.remove(_lf)

    indexer = Era5LandPercentileIndexer(
        source_dir=_source,
        target_dir=_target,
        n_cpu_cores=EnvSettings.era5_ncores(),
        n_mosaic_workers=2,
    )
    indexer.execute_indexing_pipeline()
