"""End-to-end COSMO-REA6 weather processing pipeline.

Orchestrates the full workflow, one or more months at a time:

    1. **Download** — fetch ``.grb.bz2`` files from DWD OpenData (bulk,
       all requested months/attributes in parallel). Always whole-domain
       -- DWD has no server-side area subsetting.
    2. **Decompress** — parallel bz2 decompression to raw GRIB (bulk).
    3. **Crop** (opt-in, ``crop_bbox``) — if a bbox was given (e.g. a
       country-scoped ``weather fetch``), crop every attribute to that
       bbox's ``(y, x)`` index window right here, before transform, so
       every derived-field formula below only runs over the requested
       area instead of the whole domain. See :mod:`.crop`. Skipped
       entirely when ``crop_bbox`` is ``None`` (the default).
    4. **Transform + Export** — per month (bounded peak memory): read
       GRIB with xarray/cfgrib, convert units, compute derived fields
       (GHI, DHI, T, WS_10M, ALBEDO, SNOWFALL, T_DEW, DNI), write one
       compressed NetCDF-4 file.

Each step is idempotent: re-running skips files that are already
present and have the expected size; ``resume=True`` also skips months
whose output NetCDF already exists.

Pipeline flow
-------------
::

    pipeline.run_pipeline(year=YYYY, months=[1, 2, ...])
      │
      ├── Phase 1 — Bulk download (ThreadPoolExecutor, up to ncores
      │     workers): all (month × attribute) .grb.bz2 files, verified
      │     against DWD's Content-Length afterwards.
      │
      ├── Phase 2 — Bulk decompress (ProcessPoolExecutor, up to ncores
      │     workers): all .grb.bz2 -> .grb, verified (GRIB magic bytes,
      │     expanded size) afterwards. Compressed files for the
      │     processed months are removed here when ``cleanup=True``.
      │
      └── Phase 3 — Transform + Export, sequential per month (dask uses
            all allocated cores within each month):
              transform.build_month_dataset
                  -> crop to crop_bbox's (y, x) window, if given (else
                     skipped entirely -- see .crop)
                  -> GHI, DHI, T, WS_10M, ALBEDO, SNOWFALL, T_DEW, DNI
              export.to_netcdf -> output/COSMO_REA6_<YYYY>_<MM>_all_attrs.nc
              DNI outlier report (unless skip_dni)
              cleanup: delete this month's decompressed GRIB/idx/lock
              files when ``cleanup=True``

Typical usage (from Python)::

    from weather.providers.cosmo_rea6.pipeline import run_pipeline
    nc_paths = run_pipeline(year=2018)                # all 12 months
    nc_paths = run_pipeline(year=2018, months=[6, 7])  # subset

From the CLI::

    weather run --year 2018
    weather run           # uses COSMO_YEAR from .env / env var
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..base import ADVISORY_PREFIX

if TYPE_CHECKING:
    from ...geo.bbox import BBox

logger = logging.getLogger(__name__)

# Prevent HDF5 file-locking deadlocks on network/parallel file systems (GPFS).
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")


def run_pipeline(
    year: int | None = None,
    months: list[int] | None = None,
    attributes: list[str] | None = None,
    *,
    work_dir: Path | None = None,
    ncores: int | None = None,
    include_wind_components: bool = True,
    complevel: int = 1,
    skip_download: bool = False,
    skip_decompress: bool = False,
    skip_dni: bool = False,
    resume: bool = False,
    cleanup: bool | None = None,
    crop_bbox: BBox | None = None,
) -> list[Path]:
    """Execute the COSMO-REA6 pipeline for one or more months of *year*.

    Parameters
    ----------
    year : int, optional
        Target year (default from config: 2018).
    months : list[int], optional
        Months to process, 1-12 (default: all 12).
    attributes : list[str], optional
        COSMO-REA6 attributes (default: all registered in
        ``downloaded_attributes.py``).
    work_dir : Path, optional
        Override the root working directory.
    ncores : int, optional
        Total worker budget (default: ``config["ncores"]``).
    include_wind_components : bool
        Keep raw U_10M / V_10M in each month's NetCDF (default ``True``).
    complevel : int
        zlib compression level for NetCDF (default 1).
    skip_download : bool
        Skip downloading (assume ``.grb.bz2`` files already present).
    skip_decompress : bool
        Skip decompression (assume ``.grb`` files already present).
    skip_dni : bool
        Skip the experimental per-cell DNI computation and its outlier
        report.
    resume : bool
        Skip months whose output NetCDF already exists (safe restart
        after a partial failure). Only the still-needed months are
        downloaded/decompressed.
    cleanup : bool, optional
        Remove downloaded/decompressed intermediates after a successful
        export. Default: ``config["cleanup"]`` (``COSMO_CLEANUP`` env
        var, itself defaulting to ``False`` -- keep everything).
    crop_bbox : BBox, optional
        If given, crop every month to this bbox's ``(y, x)`` index window
        right after decompress, before transform (see
        :mod:`.crop`) -- download/decompress themselves always fetch the
        whole domain (DWD has no server-side area subsetting). Default
        ``None``: no crop step runs at all, whole-domain output exactly
        as before this parameter existed.

    Returns
    -------
    list[Path]
        Paths to each month's output NetCDF (existing or freshly
        written), in the same order as *months*.

    Raises
    ------
    RuntimeError
        If any month's transform/export step fails; lists every failed
        month with its error.
    """
    from .config import get_config
    from .decompress import decompress_all, verify_decompressed
    from .download import download_all, verify_downloads
    from .export import export_netcdf
    from .naming import grib_filename
    from .transform import build_month_dataset, log_dni_stats, report_dni_outliers

    if work_dir:
        os.environ["COSMO_WORK_DIR"] = str(work_dir)

    cfg = get_config()
    year = year or cfg["year"]
    months = sorted(months or list(range(1, 13)))
    attributes = attributes or cfg["attributes"]
    ncores = ncores or cfg["ncores"]
    threads = cfg["threads_per_job"]
    cleanup = cfg["cleanup"] if cleanup is None else cleanup

    dl_dir = Path(cfg["download_dir"])
    dc_dir = Path(cfg["decompress_dir"])
    out_dir = Path(cfg["output_dir"])
    for d in (dl_dir, dc_dir, out_dir):
        d.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    logger.info("=" * 60)
    logger.info("COSMO-REA6 Weather Pipeline")
    logger.info("  Year:       %d", year)
    logger.info("  Months:     %s", ", ".join(f"{m:02d}" for m in months))
    logger.info("  Attributes: %s", attributes)
    logger.info("  Work dir:   %s", cfg["work_dir"])
    if crop_bbox is not None:
        logger.info("  Crop bbox:  %s", crop_bbox)
    logger.info("=" * 60)

    def _out_path(month: int) -> Path:
        return out_dir / f"COSMO_REA6_{year}_{month:02d}_all_attrs.nc"

    # Only download/decompress what the transform loop will actually
    # consume, so --resume doesn't re-fetch already-complete months.
    months_to_process = [
        m for m in months if not (resume and _out_path(m).exists())
    ]
    skipped_upfront = [m for m in months if m not in months_to_process]
    if skipped_upfront:
        logger.info(
            "  Skipping (--resume, already complete): %s",
            ", ".join(f"{m:02d}" for m in skipped_upfront),
        )

    do_dl = not skip_download
    do_dc = not skip_decompress

    # ── Phase 1: Bulk download ──────────────────────────────────────────
    if do_dl and months_to_process:
        t1 = time.perf_counter()
        n_workers = min(len(months_to_process) * len(attributes), ncores)
        logger.info(
            "STEP 1/3: Downloading GRIB files (%d workers)", n_workers,
        )
        download_all(
            year=year, attributes=attributes, months=months_to_process,
            dest_dir=dl_dir, max_workers=n_workers,
        )
        verify_downloads(
            year, attributes, months_to_process, dl_dir,
            max_workers=n_workers,
        )
        logger.info("  Download completed in %.1f s", time.perf_counter() - t1)
    else:
        logger.info("STEP 1/3: Download skipped")

    # ── Phase 2: Bulk decompress ─────────────────────────────────────────
    if do_dc and months_to_process:
        t2 = time.perf_counter()
        n_workers = min(len(months_to_process) * len(attributes), ncores)
        logger.info(
            "STEP 2/3: Decompressing GRIB files (%d workers)", n_workers,
        )
        decompress_all(
            src_dir=dl_dir, dest_dir=dc_dir, attributes=attributes,
            year=year, months=months_to_process, threads=threads,
            max_workers=n_workers,
        )
        verify_decompressed(
            year, attributes, months_to_process, dl_dir, dc_dir,
        )
        # bz2 deletion is gated: only after decompress is verified, and
        # only for the months just processed (another --resume run may
        # still need other months' bz2 files).
        if do_dl and cleanup:
            logger.info(
                "  Removing bz2 files for %d month(s) ...",
                len(months_to_process),
            )
            for attr in attributes:
                for m in months_to_process:
                    bz2 = dl_dir / attr / grib_filename(attr, year, m)
                    if bz2.exists():
                        try:
                            bz2.unlink()
                        except OSError as exc:
                            logger.warning(
                                "Could not delete %s: %s", bz2, exc,
                            )
        logger.info(
            "  Decompress completed in %.1f s", time.perf_counter() - t2,
        )
    else:
        logger.info("STEP 2/3: Decompress skipped")

    # ── Phase 3: Transform + Export (sequential per month) ───────────────
    logger.info("STEP 3/3: Transform + export (%d month(s))", len(months))
    nc_paths: list[Path] = []
    failed: list[tuple[int, str]] = []

    for month in months:
        out_path = _out_path(month)
        if resume and out_path.exists():
            nc_paths.append(out_path)
            continue

        t_month = time.perf_counter()
        try:
            ds_out, datasets = build_month_dataset(
                year, month, compute_dni_field=not skip_dni,
                crop_bbox=crop_bbox,
            )
            if not skip_dni:
                log_dni_stats(ds_out["DNI"], datasets["SWDIRS_RAD"])

            nc_path = export_netcdf(
                ds_out, out_path, complevel=complevel, year=year,
            )
            if not skip_dni:
                report_dni_outliers(nc_path)

            # Cleanup this month's decompressed GRIBs and cfgrib index
            # files. cfgrib names its index sidecar
            # "<grib_name>.<content-hash>.idx", not "<grib_name>.idx" --
            # glob for the hashed form too, otherwise it's silently
            # orphaned once its parent .grb is deleted (found via a real
            # rerun during this session's investigation).
            if cleanup and do_dc:
                for ds in datasets.values():
                    ds.close()
                for attr in attributes:
                    grb_name = grib_filename(attr, year, month).removesuffix(
                        ".bz2"
                    )
                    grb = dc_dir / attr / grb_name
                    candidates = [
                        grb,
                        grb.parent / (grb.name + ".idx"),
                        grb.parent / (grb.name + ".lock"),
                        *grb.parent.glob(grb.name + ".*.idx"),
                    ]
                    for p in candidates:
                        if p.exists():
                            try:
                                p.unlink()
                            except OSError as exc:
                                logger.warning(
                                    "Could not delete %s: %s", p, exc,
                                )

            month_elapsed = time.perf_counter() - t_month
            logger.info(
                "  Month %02d done  %s  (%.1f s)",
                month, nc_path.name, month_elapsed,
            )
            nc_paths.append(nc_path)
        except Exception as exc:  # noqa: BLE001 — report, continue other months
            month_elapsed = time.perf_counter() - t_month
            logger.error(
                "  Month %02d FAILED after %.1f s: %s",
                month, month_elapsed, exc, exc_info=True,
            )
            failed.append((month, str(exc)))

    # ── Final cleanup: empty per-attribute subdirectories ────────────────
    if cleanup:
        for parent in (dl_dir, dc_dir):
            if not parent.exists():
                continue
            for attr_dir in sorted(parent.iterdir()):
                if attr_dir.is_dir():
                    # non-empty (e.g. another run still using it) -> skip
                    with contextlib.suppress(OSError):
                        attr_dir.rmdir()

    elapsed = time.perf_counter() - t0
    logger.info("=" * 60)
    logger.info(
        "Pipeline complete: %d/%d month(s) (%.1f s total)",
        len(nc_paths), len(months), elapsed,
    )
    logger.info("=" * 60)

    if failed:
        raise RuntimeError(
            f"{len(failed)} month(s) failed: "
            + ", ".join(f"{m:02d} ({err})" for m, err in failed)
        )
    return nc_paths


def validate_environment() -> list[str]:
    """Check that all required tools and libraries are available.

    Returns
    -------
    list[str]
        List of issues found (empty = all OK). Advisory issues (degraded
        performance, still fully functional) are prefixed with
        `providers.base.ADVISORY_PREFIX`; everything else is a hard
        blocker.
    """
    issues: list[str] = []

    # Python packages
    for pkg in ("xarray", "cfgrib", "netCDF4", "pyproj", "eccodes"):
        try:
            __import__(pkg)
        except ImportError:
            issues.append(
                f"Python package '{pkg}' not installed.  "
                f"Install with: conda install conda-forge::{pkg}"
            )

    # External decompressors -- ADVISORY, not critical: decompress.py
    # already falls back to Python's stdlib bz2 module when neither
    # binary is found (see _detect_decompressor), just slower. Also has
    # no win-64 conda-forge build (same gap as `cdo`, see weather_env.yml's
    # comment next to both) -- expected to be permanently absent on a
    # Windows dev machine, not a misconfiguration to fix there.
    for cmd in ("pbzip2", "lbzip2"):
        if shutil.which(cmd):
            break
    else:
        issues.append(
            ADVISORY_PREFIX + "No parallel decompressor found (pbzip2 or "
            "lbzip2) -- falling back to the slower Python bz2 module. "
            "Expected on Windows (no win-64 conda-forge build for "
            "either); on Linux, install with: "
            "conda install conda-forge::pbzip2"
        )

    # Config sanity
    from .config import get_config
    cfg = get_config()
    if cfg["year"] < 1995 or cfg["year"] > 2019:
        issues.append(
            f"COSMO_YEAR={cfg['year']} is outside COSMO-REA6 range "
            "(1995-2019)."
        )

    # Path validation and writable checks
    path_issues = _validate_paths(cfg)
    issues.extend(path_issues)

    return issues


def _validate_paths(cfg: dict) -> list[str]:
    """Validate that all required directories exist and are writable.

    Parameters
    ----------
    cfg : dict
        Pipeline configuration dictionary.

    Returns
    -------
    list[str]
        List of path validation issues (empty = all OK).
    """
    issues: list[str] = []

    # Check work directory
    work_dir = cfg["work_dir"]
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as exc:
        issues.append(
            f"Cannot create work directory {work_dir}: {exc}"
        )
        return issues

    # Check subdirectories are writable
    for dir_key in ("download_dir", "decompress_dir", "output_dir"):
        dir_path = Path(cfg[dir_key])
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            issues.append(
                f"Cannot create {dir_key} {dir_path}: {exc}"
            )
            continue

        # Test writability by creating a temporary file
        try:
            test_file = dir_path / ".write_test"
            test_file.touch()
            test_file.unlink()
        except (OSError, PermissionError) as exc:
            issues.append(
                f"Directory {dir_path} is not writable: {exc}"
            )

    # Check available disk space (require at least 10GB free)
    # Only on Unix-like systems (Linux, macOS), not on Windows
    if hasattr(os, 'statvfs'):
        try:
            stat = os.statvfs(work_dir)
            free_gb = stat.f_bavail * stat.f_frsize / (1024 ** 3)
            if free_gb < 10:
                issues.append(
                    f"Insufficient disk space on {work_dir}: "
                    f"{free_gb:.1f} GB free (minimum 10 GB required)"
                    )
        except (OSError, AttributeError):
            # statvfs not available on Windows, skip disk space check
            pass

    return issues
