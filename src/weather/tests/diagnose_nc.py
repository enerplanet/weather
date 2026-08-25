#!/usr/bin/env python3
"""Diagnose a single ERA5-Land monthly NetCDF.

Reports:

1. **Time axes**    — span, and whether ``time`` and ``valid_time`` agree.
2. **Value profile** — per variable: %NaN (ocean), %zero, %finite-nonzero,
                       min and max.
3. **Spot check**   — a time series at one grid cell for the key
                       variables (T, GHI, sf, sp, RH, WS_10M, sde).
4. **Boundary**     — the first stamp of the accumulated variables
                       (GHI, sf), flagged as repaired or unrepaired.

Probe a specific location with ``--lat`` / ``--lon``.  For summer months
an ARCTIC point (e.g. 69 N, 25 E) is the discriminating test: with the
midnight sun the 00:00 hour must carry a REAL, non-zero GHI — so it
exposes a boundary that was wrongly zeroed or left NaN.

Usage::

    python diagnose_nc.py FILE.nc
    python diagnose_nc.py FILE.nc --lat 69.0 --lon 25.0
    python diagnose_nc.py FILE.nc --lat 52.0 --lon 5.0 --hours 24
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

#: A plausible hourly GHI flux never exceeds this (W/m^2); the solar
#: constant is ~1361 and surface GHI tops out around 1100-1200.
_MAX_PLAUSIBLE_GHI = 1400.0

#: Variables shown in the spot check, in order.
_SPOT_VARS = ("T", "GHI", "sf", "RH", "WS_10M", "sp", "sde")

#: Accumulated variables whose first stamp needs the cross-month repair.
_ACCUM_VARS = ("GHI", "sf")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", help="Path to the monthly .nc file")
    ap.add_argument(
        "--lat", type=float, default=None,
        help="Latitude of the cell to probe (default: grid centre)",
    )
    ap.add_argument(
        "--lon", type=float, default=None,
        help="Longitude of the cell to probe (0-360 or -180..180)",
    )
    ap.add_argument(
        "--hours", type=int, default=12,
        help="How many leading timesteps to print in the spot check",
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"not found: {path}")

    import xarray as xr

    ds = xr.open_dataset(path)
    try:
        print(f"File: {path.name}")
        print(f"dims: {dict(ds.sizes)}")
        status = ds.attrs.get("boundary_status", "(none)")
        print(f"boundary_status: {status}\n")

        # ---------------- time axes ----------------
        print("=" * 66)
        print("TIME AXES")
        print("=" * 66)
        t = pd.DatetimeIndex(np.asarray(ds["time"].values))
        print(f"  first : {t[0]}")
        print(f"  last  : {t[-1]}")
        print(f"  count : {len(t)}")
        if "valid_time" in ds.variables:
            vt = pd.DatetimeIndex(np.asarray(ds["valid_time"].values))
            print(f"  time == valid_time? {bool(np.array_equal(t, vt))}")
        print()

        # ---------------- value profile ----------------
        print("=" * 66)
        print("VALUE PROFILE")
        print("=" * 66)
        print(f"  {'var':<9s} {'%NaN':>7s} {'%zero':>7s} {'%finite!=0':>11s} "
              f"{'min':>12s} {'max':>12s}")
        for v in ds.data_vars:
            a = ds[v].values
            if a.ndim < 3:
                continue
            total = a.size
            nan = np.isnan(a).sum()
            finite = np.isfinite(a)
            zero = int((finite & (a == 0)).sum())
            nonzero = int((finite & (a != 0)).sum())
            allnan = nan == total
            mn = float("nan") if allnan else float(np.nanmin(a))
            mx = float("nan") if allnan else float(np.nanmax(a))
            print(
                f"  {v:<9s} {100*nan/total:>6.1f}% {100*zero/total:>6.1f}% "
                f"{100*nonzero/total:>10.1f}% {mn:>12.4g} {mx:>12.4g}"
            )
        print()

        # ---------------- boundary check ----------------
        print("=" * 66)
        print("BOUNDARY (first stamp)")
        print("=" * 66)
        for v in _ACCUM_VARS:
            if v not in ds:
                continue
            first = ds[v].isel(time=0).values
            if np.all(np.isnan(first)):
                print(f"  {v:<5s} all-NaN  -> archive start (no predecessor)")
                continue
            mx = float(np.nanmax(first))
            if v == "GHI" and mx > _MAX_PLAUSIBLE_GHI:
                verdict = (
                    "UNREPAIRED — raw daily total, not an hourly flux. "
                    "Run providers/era5_land/boundary_repair.py!"
                )
            else:
                verdict = "repaired / plausible"
            print(f"  {v:<5s} max={mx:>10.2f}  -> {verdict}")
        print()

        # ---------------- spot check ----------------
        print("=" * 66)
        print("SPOT CHECK")
        print("=" * 66)
        if args.lat is not None and args.lon is not None \
                and "latitude" in ds.coords:
            latv = np.asarray(ds["latitude"].values)
            lonv = np.asarray(ds["longitude"].values)
            lon_t = args.lon % 360.0 if lonv.max() > 180.0 else args.lon
            iy = int(np.abs(latv - args.lat).argmin())
            ix = int(np.abs(lonv - lon_t).argmin())
            print(
                f"  cell nearest lat={args.lat}, lon={args.lon} -> "
                f"y={iy}, x={ix} "
                f"(actual lat={latv[iy]:.2f}, lon={lonv[ix]:.2f})"
            )
        else:
            iy, ix = ds.sizes["y"] // 2, ds.sizes["x"] // 2
            print(f"  grid-centre cell y={iy}, x={ix}")
            print("  (TIP: --lat 69.0 --lon 25.0 probes an Arctic cell,")
            print("        where summer midnight sun makes 00:00 non-zero)")

        n = min(args.hours, ds.sizes["time"])
        print(f"  first {n} timesteps ({t[0]} ...):\n")
        for v in _SPOT_VARS:
            if v not in ds:
                continue
            series = ds[v].isel(y=iy, x=ix).values[:n]
            txt = np.array2string(
                series, precision=2, max_line_width=200,
                formatter={"float_kind": lambda x: f"{x:9.2f}"},
            )
            print(f"  {v:<7s}: {txt}")
        print()
        if "GHI" in ds:
            g0 = float(ds["GHI"].isel(y=iy, x=ix, time=0).values)
            if np.isnan(g0):
                print("  NOTE: GHI[0] is NaN at this cell — ocean, or the "
                      "archive's first month.")
            elif g0 > _MAX_PLAUSIBLE_GHI:
                print("  WARNING: GHI[0] is an absurd flux — this file is "
                      "UNREPAIRED.")
            else:
                print(f"  GHI[0] = {g0:.2f} W/m^2 — a plausible hourly flux.")
    finally:
        ds.close()


if __name__ == "__main__":
    main()
