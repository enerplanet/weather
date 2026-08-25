#!/usr/bin/env python3
"""Verify a set of ERA5-Land monthly NetCDFs: hours, boundaries, continuity.

Checks, for every monthly file in a folder:

1. **Hour count**   — does it hold exactly 24 x days_in_month stamps?
2. **Span**         — does it run <1st> 00:00 .. <last> 23:00?
3. **Continuity**   — does each file start exactly one hour after the
                      previous file ends (no gap, no overlap)?
4. **Boundary**     — is the first stamp of GHI/sf a plausible hourly
                      flux (repaired) or an absurd raw daily total
                      (unrepaired)?  Reports ``boundary_status``.
5. **NaN profile**  — per-variable NaN fraction (ocean) and any land NaN.

Run it BEFORE and AFTER providers/era5_land/boundary_repair.py (or a
run_pipeline() call, which now runs it automatically) to see the
difference.

Usage::

    python verify_months.py                    # uses ERA5 output_dir
    python verify_months.py D:/.../era5_land/output
    python verify_months.py <dir> --lat 69.0 --lon 25.0   # Arctic probe

If ``output_dir`` is omitted, it defaults to the same
``<ERA5_WORK_DIR>/output`` folder that ``test_era5_one_month.py`` /
``test_era5_one_year.py`` / ``test_era5_multi_year.py`` write to (resolved
from ``.env`` / ``ERA5_WORK_DIR`` via
:func:`weather.providers.era5_land.config.get_config`).
"""

from __future__ import annotations

import argparse
import calendar
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from weather.providers.era5_land.config import get_config

_PATTERN = re.compile(r"ERA5_LAND_(\d{4})_(\d{2})_all_attrs\.nc$")

#: A repaired hourly GHI flux should never exceed this (W/m^2).
#: The solar constant is ~1361; surface GHI tops out ~1100-1200.
_MAX_PLAUSIBLE_GHI = 1400.0


def main() -> None:
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
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    args = ap.parse_args()

    out_dir = (
        Path(args.output_dir) if args.output_dir is not None
        else get_config()["output_dir"]
    )
    if not out_dir.is_dir():
        sys.exit(f"not a directory: {out_dir}")

    import xarray as xr

    files = []
    for p in out_dir.glob("ERA5_LAND_*_all_attrs.nc"):
        m = _PATTERN.search(p.name)
        if m:
            files.append((int(m.group(1)), int(m.group(2)), p))
    files.sort(key=lambda r: (r[0], r[1]))
    if not files:
        sys.exit("No monthly files found.")

    print(f"Found {len(files)} monthly file(s) in {out_dir}\n")

    print("=" * 78)
    print(f"{'file':<12s} {'stamps':>7s} {'expect':>7s} {'first stamp':<17s} "
          f"{'last stamp':<17s} {'ok':>4s}")
    print("=" * 78)

    prev_last = None
    prev_label = None
    rows = []

    for idx, (yr, mo, path) in enumerate(files):
        ds = xr.open_dataset(path)
        try:
            t = pd.DatetimeIndex(np.asarray(ds["time"].values))
            n = len(t)
            ndays = calendar.monthrange(yr, mo)[1]
            expect = 24 * ndays

            want_first = pd.Timestamp(yr, mo, 1, 0)
            want_last = pd.Timestamp(yr, mo, ndays, 23)

            # The ARCHIVE's very first month is a legitimate special case:
            # there is no preceding forecast day to supply the <1st> 00:00
            # stamp, so ERA5-Land ships 743 hours starting at 01:00.
            is_archive_start = idx == 0
            archive_ok = (
                is_archive_start
                and n == expect - 1
                and t[0] == want_first + pd.Timedelta(hours=1)
                and t[-1] == want_last
            )
            ok = (
                n == expect and t[0] == want_first and t[-1] == want_last
            ) or archive_ok

            note = "  (archive start: no 00:00 stamp)" if archive_ok else ""
            print(
                f"{yr}-{mo:02d}     {n:>7d} {expect:>7d} "
                f"{str(t[0])[:16]:<17s} {str(t[-1])[:16]:<17s} "
                f"{'OK' if ok else 'FAIL':>4s}{note}"
            )

            # continuity with the previous file
            if prev_last is not None:
                delta = (t[0] - prev_last).total_seconds() / 3600.0
                if delta != 1.0:
                    print(
                        f"    !! discontinuity: {prev_label} ends "
                        f"{prev_last}, {yr}-{mo:02d} starts {t[0]} "
                        f"(gap {delta:.0f} h; expected 1 h)"
                    )
            prev_last = t[-1]
            prev_label = f"{yr}-{mo:02d}"

            rows.append((yr, mo, path, ds.attrs.get("boundary_status", "")))
        finally:
            ds.close()

    # ---------------- boundary status ----------------
    print()
    print("=" * 78)
    print("BOUNDARY (first stamp of each file)")
    print("=" * 78)
    print(f"  {'file':<10s} {'var':<5s} {'first-stamp max':>16s} "
          f"{'verdict':<28s}")

    for yr, mo, path, status in rows:
        ds = xr.open_dataset(path)
        try:
            for var in ("GHI", "sf"):
                if var not in ds:
                    continue
                first = ds[var].isel(time=0).values
                if np.all(np.isnan(first)):
                    verdict = "all-NaN (archive start)"
                    mx = float("nan")
                else:
                    mx = float(np.nanmax(first))
                    if var == "GHI" and mx > _MAX_PLAUSIBLE_GHI:
                        verdict = "UNREPAIRED (raw daily total)"
                    else:
                        verdict = "repaired / plausible"
                print(
                    f"  {yr}-{mo:02d}    {var:<5s} {mx:>16.1f}  {verdict:<28s}"
                )
            short = status.split(":")[0] if status else "(none)"
            print(f"             status: {short}")
        finally:
            ds.close()

    # ---------------- optional point probe ----------------
    if args.lat is not None and args.lon is not None:
        print()
        print("=" * 78)
        print(f"POINT PROBE  lat={args.lat}  lon={args.lon}")
        print("=" * 78)
        for yr, mo, path, _ in rows:
            ds = xr.open_dataset(path)
            try:
                if "latitude" not in ds.coords:
                    break
                latv = np.asarray(ds["latitude"].values)
                lonv = np.asarray(ds["longitude"].values)
                lon_t = args.lon % 360.0 if lonv.max() > 180.0 else args.lon
                iy = int(np.abs(latv - args.lat).argmin())
                ix = int(np.abs(lonv - lon_t).argmin())
                g = ds["GHI"].isel(y=iy, x=ix).values[:6]
                print(
                    f"  {yr}-{mo:02d}  GHI first 6 h: "
                    + np.array2string(
                        g, precision=2, max_line_width=200,
                        formatter={"float_kind": lambda v: f"{v:9.2f}"},
                    )
                )
            finally:
                ds.close()
        print()
        print("  (first value should be a small hourly flux after repair —")
        print("   NOT thousands of W/m^2, and NOT forced to zero.)")

    # ---------------- NaN profile of the last file ----------------
    print()
    print("=" * 78)
    print("NaN PROFILE (last file — ocean should dominate)")
    print("=" * 78)
    yr, mo, path, _ = rows[-1]
    ds = xr.open_dataset(path)
    try:
        print(f"  {yr}-{mo:02d}")
        print(f"    {'var':<8s} {'%NaN':>7s} {'min':>12s} {'max':>12s}")
        for v in ds.data_vars:
            a = ds[v].values
            if a.ndim < 3:
                continue
            nan = 100.0 * np.isnan(a).mean()
            mn = np.nanmin(a) if not np.all(np.isnan(a)) else float("nan")
            mx = np.nanmax(a) if not np.all(np.isnan(a)) else float("nan")
            print(f"    {v:<8s} {nan:>6.1f}% {mn:>12.4g} {mx:>12.4g}")
    finally:
        ds.close()


if __name__ == "__main__":
    main()
