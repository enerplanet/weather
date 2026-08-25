"""Repair the month-boundary hour across ERA5-Land NetCDFs.

Runs automatically as the last phase of :func:`~weather.providers.
era5_land.pipeline.run_pipeline`, and can also be invoked directly as a
script over an arbitrary output folder. This is a mandatory step for
every accumulated variable's first hourly stamp -- it must complete
before any analysis (percentile indexing, point queries, etc.) touches
the archive.

Background
----------
ERA5-Land's monthly GRIB is laid out as (verified via enumerate_month.py)::

    <prev last day>, step 24        -> <1st> 00:00        (1 message)
    <1st>..<last-1>, steps 1..24    -> hourly stamps
    <last day>,      steps 1..23    -> ... <last> 23:00

The transform drops the phantom (all-NaN) stamps, leaving exactly the
calendar month ``<1st> 00:00 .. <last> 23:00``. The FIRST stamp is the
previous month's final hour and arrives WITHOUT its step-23 predecessor,
so it cannot be de-accumulated within one file. The transform therefore
leaves the RAW accumulated daily total / 3600 at that stamp — an absurd
"flux" (thousands of W/m^2) that MUST be repaired before use.

The repair, using only data already in the files
------------------------------------------------
Because the accumulation resets daily, the missing predecessor
(raw step 23) equals the SUM of the previous day's first 23 hourly
increments — i.e. exactly the previous month's values at its last day's
stamps 01:00..23:00. Hence::

    value(<1st> 00:00) = stored_first
                         - sum(prev month's values at <prev last day>
                               01:00 .. 23:00)

No GRIB re-reading. Applied to every accumulated variable (GHI and sf).

The archive's very FIRST month has no predecessor anywhere on disk: its
first stamp is not a usable hourly flux, so it is blanked in the main
variable — but see "Never destructive" below.

Predecessor lookup is always disk-based, not list-adjacency-based
-------------------------------------------------------------------
Every month's predecessor is looked up directly in *output_dir* by its
computed (year, month) filename, regardless of whether that predecessor
file was created in this run/session or falls outside a requested
``from_year``/``to_year``/``months`` filter. This makes repairing
partial ranges (or a specific list of months) always safe: a month is
only ever treated as the true archive start if its predecessor genuinely
does not exist ANYWHERE in *output_dir* (checked against the whole
folder, not just the current filter) — never merely because it happens
to be first in a filtered subset.

Never destructive
------------------
Before a variable's first-hour value is ever modified (repaired OR
blanked as an archive start), the ORIGINAL stored value is preserved
verbatim in a new sibling variable ``<var>_boundary_raw`` (created once,
never overwritten). This means:

* A repair can always be recomputed/audited later from the true raw
  source, even after the visible variable has been modified.
* If a file was ever mis-repaired (e.g. by an older version of this
  module that assumed list-adjacency), it self-heals automatically the
  next time it runs: it recomputes from ``<var>_boundary_raw``, not from
  the already-modified value.

Usage
-----
Called automatically by ``run_pipeline()`` after transform+export, for
just the months that run touched (predecessors are still looked up
disk-wide, so this is always safe). To repair an arbitrary folder
directly::

    python boundary_repair.py                    # uses ERA5 output_dir
    python boundary_repair.py /data/soma/era5_land/output
    python boundary_repair.py <dir> --dry-run
    python boundary_repair.py <dir> --from-year 1950 --to-year 2025
    python boundary_repair.py <dir> --months 2019-01 2020-06

``--months`` repairs only the exact months given (space-separated
``YYYY-MM`` tokens) instead of a ``--from-year``/``--to-year`` range —
useful for touching up a handful of months in a large multi-year
archive without re-scanning/re-writing everything else. Each requested
month still looks up its true predecessor across the whole folder, so
the months need not be contiguous or in any particular order.

If ``output_dir`` is omitted, it defaults to the same
``<ERA5_WORK_DIR>/output`` folder that ``test_era5_one_month.py`` /
``test_era5_one_year.py`` / ``test_era5_multi_year.py`` write to (resolved
from ``.env`` / ``ERA5_WORK_DIR`` via
:func:`weather.providers.era5_land.config.get_config`).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

logger = logging.getLogger("boundary_repair")

_PATTERN = re.compile(r"ERA5_LAND_(\d{4})_(\d{2})_all_attrs\.nc$")
_MONTH_TOKEN = re.compile(r"^(\d{4})-(\d{2})$")

#: Accumulated variables whose first stamp needs the cross-month repair.
_REPAIR_VARS = ("GHI", "sf")

#: Physically impossible hourly mean irradiance, W/m^2. The solar
#: constant is ~1361 W/m^2, so an interior stamp above this is
#: corrupt rather than merely extreme -- see ``spike_repair``.
_IMPLAUSIBLE_GHI_W_M2 = 1500.0

#: Written into ``boundary_status`` once a file has been repaired.
_DONE_FLAG = "BOUNDARY_REPAIRED"


def _backup_var(var: str) -> str:
    """Name of the sibling variable preserving *var*'s original raw
    first-hour value (see module docstring, "Never destructive")."""
    return f"{var}_boundary_raw"


def _is_repaired(ds) -> bool:
    """True only if this file has actually been repaired.

    NB: we look for a dedicated flag, NOT the substring "REPAIRED" — the
    transform writes ``boundary_status = "UNREPAIRED: ..."`` and
    ``"REPAIRED" in "UNREPAIRED"`` is True, which previously caused every
    fresh file to be skipped.
    """
    return str(ds.attrs.get("boundary_status", "")).startswith(_DONE_FLAG)


def _discover(out_dir: Path, y0: int | None, y1: int | None):
    """Monthly files sorted chronologically as (year, month, path)."""
    found = []
    for p in out_dir.glob("ERA5_LAND_*_all_attrs.nc"):
        m = _PATTERN.search(p.name)
        if not m:
            continue
        yr, mo = int(m.group(1)), int(m.group(2))
        if y0 is not None and yr < y0:
            continue
        if y1 is not None and yr > y1:
            continue
        found.append((yr, mo, p))
    found.sort(key=lambda r: (r[0], r[1]))
    return found


def _predecessor(year: int, month: int) -> tuple[int, int]:
    """Return the (year, month) immediately before *year*-*month*."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _find_on_disk(out_dir: Path, year: int, month: int) -> Path | None:
    """Return the monthly file for *year*-*month* if it exists anywhere
    in *out_dir* — independent of any ``from_year``/``to_year``/
    ``months`` filter.
    """
    p = out_dir / f"ERA5_LAND_{year}_{month:02d}_all_attrs.nc"
    return p if p.exists() else None


def _needs_redo(status: str, has_predecessor: bool, is_earliest: bool) -> bool:
    """True if a file previously marked "archive start" should be
    re-repaired now, because a real predecessor exists on disk and this
    is not genuinely the earliest file in the whole archive.
    """
    return "archive start" in status and has_predecessor and not is_earliest


def _prev_lastday_sum(prev_ds, var: str):
    """Sum of *var* over the previous file's LAST calendar day,
    stamps 01:00..23:00 (the first 23 increments of that day).

    Returns (array, n_stamps). ``array`` is None if the window is not
    exactly 23 stamps (i.e. the previous file is malformed).
    """

    t = pd.DatetimeIndex(np.asarray(prev_ds["time"].values))
    last_day = t[-1].floor("D")
    sel = (t >= last_day + pd.Timedelta(hours=1)) & (
        t <= last_day + pd.Timedelta(hours=23)
    )
    n = int(sel.sum())
    if n != 23:
        return None, n
    sub = prev_ds[var].isel(time=np.where(sel)[0])
    # skipna=False: ocean (NaN) stays NaN; a land cell with a missing hour
    # also goes NaN, which is the honest outcome.
    return sub.sum(dim="time", skipna=False).load().values, n


def _raw_first_stamp(cur, var: str):
    """Return the ORIGINAL, never-modified first-hour value of *var*.

    Reads from the ``<var>_boundary_raw`` backup if this file has been
    touched before (so a prior — possibly buggy — repair can never
    poison a later one); otherwise reads (and does not yet persist) the
    current value, which at this point is still the untouched raw value.
    """

    backup = _backup_var(var)
    if backup in cur:
        return np.asarray(cur[backup].values)
    return np.asarray(cur[var].isel(time=0).load().values)


def _repair_first_stamp(cur, prev, var: str):
    """Return (new_first_2d, n_repaired) for *var*, or (None, 0)."""

    if var not in cur or var not in prev:
        return None, 0

    prev_sum, n = _prev_lastday_sum(prev, var)
    if prev_sum is None:
        logger.warning(
            "  %s: previous file's last day has %d stamps in 01:00..23:00 "
            "(expected 23) — cannot repair.", var, n,
        )
        return None, 0

    stored_first = _raw_first_stamp(cur, var)

    # Guard: the two files must share the same spatial grid. A mismatch
    # means they were downloaded with different 'area' settings.
    if stored_first.shape != prev_sum.shape:
        logger.error(
            "  %s: GRID MISMATCH — current %s vs previous %s. These "
            "files were produced with different bounding boxes; skipping. "
            "Re-download/re-transform them with a consistent ERA5_AREA.",
            var, stored_first.shape, prev_sum.shape,
        )
        return None, 0

    increment = stored_first - prev_sum

    neg = np.isfinite(increment) & (increment < -1.0)
    if neg.any():
        logger.warning(
            "  %s: %d cell(s) give a negative boundary increment "
            "(min %.3f) — clipping to 0; inspect those cells.",
            var, int(neg.sum()), float(np.nanmin(increment)),
        )
    increment = np.where(
        np.isfinite(increment), np.clip(increment, 0.0, None), np.nan
    )
    return increment, int(np.isfinite(increment).sum())


def _ensure_backup(ds, var: str) -> None:
    """Create ``<var>_boundary_raw`` from the current (still-untouched)
    first-hour value, unless it already exists — never overwritten once
    created, so the true original value is always recoverable.
    """
    backup = _backup_var(var)
    if backup in ds or var not in ds:
        return
    raw0 = ds[var].isel(time=0).load().values.copy()
    spatial_dims = ds[var].dims[1:]
    ds[backup] = (spatial_dims, raw0)
    ds[backup].attrs["description"] = (
        f"Original UNREPAIRED first-hour value of {var}, preserved "
        "so the boundary repair is always re-derivable/auditable and "
        "never destroys real downloaded data."
    )


def _warn_if_implausible(ds, tag: str) -> int:
    """Log any physically impossible GHI left at an *interior* stamp.

    This repair only ever touches stamp 0, so a corrupt interior stamp
    would otherwise ship silently -- see
    :mod:`~weather.providers.era5_land.spike_repair` for the mechanism
    and the fix. Checking is essentially free here because the caller
    has already loaded the whole file in order to rewrite it.

    Parameters
    ----------
    ds : xarray.Dataset
        The loaded monthly file, after its boundary repair.
    tag : str
        Human-readable file identifier for the log line.

    Returns
    -------
    int
        Number of impossible values found (0 when the file is clean).
    """
    if "GHI" not in ds:
        return 0
    interior = np.nan_to_num(ds["GHI"].values[1:], nan=0.0)
    if interior.size == 0:
        return 0
    bad = int((interior > _IMPLAUSIBLE_GHI_W_M2).sum())
    if bad:
        logger.error(
            "%s: %d interior GHI value(s) above %.0f W/m^2 (max %.0f) "
            "— not a boundary defect; run spike_repair to correct them "
            "from the cached raw GRIB.",
            tag, bad, _IMPLAUSIBLE_GHI_W_M2, float(interior.max()),
        )
    return bad


def _write(ds, path: Path) -> None:
    """Atomically write *ds* back to *path* with the standard encoding."""
    enc = {
        v: {"zlib": True, "complevel": 1, "dtype": "float32"}
        for v in ds.data_vars
    }
    tmp = path.with_suffix(".nc.tmp")
    ds.to_netcdf(tmp, encoding=enc, format="NETCDF4", engine="netcdf4")
    ds.close()
    tmp.replace(path)


def _parse_months(tokens: list[str]) -> set[tuple[int, int]]:
    wanted = set()
    for tok in tokens:
        m = _MONTH_TOKEN.match(tok)
        if not m:
            sys.exit(f"--months token must be 'YYYY-MM', got: {tok!r}")
        wanted.add((int(m.group(1)), int(m.group(2))))
    return wanted


def repair_boundaries(
    output_dir: Path,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    months: set[tuple[int, int]] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Repair the month-boundary hour across *output_dir*'s monthly files.

    Parameters
    ----------
    output_dir : Path
        Folder of ``ERA5_LAND_<YYYY>_<MM>_all_attrs.nc`` files.
    from_year, to_year : int, optional
        Restrict to a year range (ignored if *months* is given).
    months : set of (year, month), optional
        Repair only these exact months instead of a year range. Each
        month's predecessor is still looked up across the whole
        *output_dir*, so the set need not be contiguous.
    dry_run : bool
        Log what would change without writing anything.

    Returns
    -------
    dict
        ``{"repaired": int, "skipped": int, "gaps": int, "errors": int}``.
    """

    output_dir = Path(output_dir)
    all_files = _discover(output_dir, None, None)
    if not all_files:
        logger.warning("No monthly files found in %s", output_dir)
        return {"repaired": 0, "skipped": 0, "gaps": 0, "errors": 0}
    earliest = (all_files[0][0], all_files[0][1])

    if months:
        by_key = {(y, m): p for y, m, p in all_files}
        missing = sorted(months - set(by_key))
        if missing:
            raise FileNotFoundError(
                f"Requested months not found in {output_dir}: "
                f"{[f'{y}-{m:02d}' for y, m in missing]}"
            )
        files = [(y, m, by_key[(y, m)]) for y, m in sorted(months)]
    else:
        files = _discover(output_dir, from_year, to_year)
        if not files:
            logger.warning("No monthly files found in the given year range.")
            return {"repaired": 0, "skipped": 0, "gaps": 0, "errors": 0}

    logger.info("Checking %d monthly file(s) in %s.", len(files), output_dir)

    repaired = skipped = gaps = errors = 0

    for cy, cm, cpath in files:
        py, pm = _predecessor(cy, cm)
        ppath = _find_on_disk(output_dir, py, pm)
        is_earliest = (cy, cm) == earliest

        with xr.open_dataset(cpath) as probe:
            status = str(probe.attrs.get("boundary_status", ""))
            already_ok = _is_repaired(probe)
            redo = _needs_redo(status, ppath is not None, is_earliest)

        if already_ok and not redo:
            logger.info("%d-%02d: already repaired.", cy, cm)
            skipped += 1
            continue

        if ppath is None:
            if is_earliest:
                # Genuine archive start: no predecessor exists anywhere
                # on disk. Blank the main variable's first stamp (it is
                # not a usable hourly flux), but the true raw value
                # lives on forever in <var>_boundary_raw.
                if dry_run:
                    logger.info(
                        "%d-%02d (archive start): first stamp -> NaN "
                        "[dry-run]", cy, cm,
                    )
                    continue
                ds = xr.open_dataset(cpath).load()
                for var in _REPAIR_VARS:
                    if var in ds:
                        _ensure_backup(ds, var)
                        vals = ds[var].values
                        vals[0] = np.nan
                        ds[var] = (ds[var].dims, vals, ds[var].attrs)
                ds.attrs["boundary_status"] = (
                    f"{_DONE_FLAG}: archive start — first stamp set to "
                    "NaN (no predecessor month exists anywhere on disk; "
                    "original value preserved in <var>_boundary_raw)"
                )
                _write(ds, cpath)
                logger.info(
                    "%d-%02d (archive start): first stamp -> NaN", cy, cm,
                )
                repaired += 1
            else:
                logger.warning(
                    "%d-%02d: predecessor %d-%02d not found anywhere in "
                    "%s, and this is not the archive's earliest file "
                    "(%d-%02d) — real gap; leaving UNREPAIRED.",
                    cy, cm, py, pm, output_dir, *earliest,
                )
                gaps += 1
            continue

        if redo:
            logger.warning(
                "%d-%02d: was wrongly marked archive-start; predecessor "
                "%d-%02d now on disk — re-repairing from the preserved "
                "raw value.",
                cy, cm, py, pm,
            )

        cur = xr.open_dataset(cpath).load()
        prev = xr.open_dataset(ppath)
        try:
            t0 = str(np.asarray(cur["time"].values)[0])[:16]
            changed = False
            for var in _REPAIR_VARS:
                if var not in cur:
                    continue
                _ensure_backup(cur, var)
                new_first, n_ok = _repair_first_stamp(cur, prev, var)
                if new_first is None:
                    errors += 1
                    continue
                logger.info(
                    "%d-%02d  %-4s @ %s: %d cell(s) repaired%s",
                    cy, cm, var, t0, n_ok,
                    "  [dry-run]" if dry_run else "",
                )
                if not dry_run:
                    vals = cur[var].values
                    vals[0] = new_first
                    cur[var] = (cur[var].dims, vals, cur[var].attrs)
                    cur[var].attrs["boundary_repaired"] = (
                        f"first stamp computed from {ppath.name}"
                    )
                    changed = True

            if changed:
                cur.attrs["boundary_status"] = (
                    f"{_DONE_FLAG}: first stamp computed from "
                    f"{ppath.name} (previous last-day increment sum; "
                    "original value preserved in <var>_boundary_raw)"
                )
                errors += _warn_if_implausible(cur, f"{cy}-{cm:02d}")
                _write(cur, cpath)
                repaired += 1
        finally:
            prev.close()
            with contextlib.suppress(Exception):
                cur.close()

    logger.info("=" * 60)
    logger.info("Boundary repair complete.")
    logger.info("  repaired : %d", repaired)
    logger.info("  skipped  : %d (already repaired)", skipped)
    if gaps:
        logger.warning("  gaps     : %d (missing predecessor month)", gaps)
    if errors:
        logger.warning("  errors   : %d (see messages above)", errors)
    logger.info("=" * 60)

    return {
        "repaired": repaired, "skipped": skipped,
        "gaps": gaps, "errors": errors,
    }


def main() -> None:
    # Absolute import (not relative `.config`) so this module keeps
    # working when run as a direct script path (`python
    # .../boundary_repair.py`), not just via `python -m
    # weather.providers.era5_land.boundary_repair` -- a relative import
    # here breaks under direct-file-path invocation with "attempted
    # relative import with no known parent package" (confirmed the hard
    # way: percentile_index.py has this exact latent bug today).
    from weather.providers.era5_land.config import get_config

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "output_dir", nargs="?", default=None,
        help=(
            "Folder of monthly ERA5_LAND_*_all_attrs.nc files. "
            "Default: <ERA5_WORK_DIR>/output (same as the era5_land "
            "pipeline's output_dir, from .env / ERA5_WORK_DIR)."
        ),
    )
    ap.add_argument("--from-year", type=int, default=None)
    ap.add_argument("--to-year", type=int, default=None)
    ap.add_argument(
        "--months", nargs="+", default=None, metavar="YYYY-MM",
        help=(
            "Repair only these exact months (e.g. --months 2019-01 "
            "2020-06), instead of a --from-year/--to-year range. Each "
            "month's predecessor is still looked up across the whole "
            "output_dir, so the list need not be contiguous."
        ),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir = (
        Path(args.output_dir) if args.output_dir is not None
        else get_config()["output_dir"]
    )
    if not out_dir.is_dir():
        sys.exit(f"not a directory: {out_dir}")
    logger.info("Output dir: %s", out_dir)

    months = _parse_months(args.months) if args.months else None
    repair_boundaries(
        out_dir,
        from_year=args.from_year,
        to_year=args.to_year,
        months=months,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
