#!/usr/bin/env python3
"""Diagnose a single COSMO-REA6 monthly NetCDF.

Mirrors ``diagnose_nc.py``'s scope (that one is ERA5-Land-specific; COSMO
had no equivalent single-file spot-check tool). Reports:

1. **Attribute completeness** — which of the canonical cross-provider
   variables (see ``## cross-provider`` in ``CLAUDE.md``) are present.
2. **Value profile**          — per variable: %NaN, %zero, %finite-nonzero,
                                 min/max/mean.
3. **Spot check**             — a time series at one grid cell for the key
                                 variables.

COSMO has no ``boundary_status`` attribute (that repair is ERA5-Land-only)
and no accumulated-variable boundary artifact to check.

Usage::

    python inspect_cosmo_nc.py FILE.nc
    python inspect_cosmo_nc.py FILE.nc --lat 52.0 --lon 5.0 --hours 24
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

#: Canonical variables COSMO-REA6's transform.py is expected to produce
#: (see CLAUDE.md's "## cross-provider" naming-unification writeup).
_EXPECTED_VARS = (
    "T", "GHI", "DHI", "DNI", "T_DEW", "ALBEDO", "SNOWFALL",
    "SNOW_DEPTH", "U_10M", "V_10M", "WS_10M", "RH", "PS",
)

#: Variables shown in the spot check, in order.
_SPOT_VARS = ("T", "GHI", "DHI", "DNI", "RH", "WS_10M", "PS")


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
        help="Longitude of the cell to probe",
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
        has_latlon = "latitude" in ds.coords and "longitude" in ds.coords
        print(f"has lat/lon coords: {has_latlon}\n")

        # ---------------- time axis ----------------
        print("=" * 66)
        print("TIME AXIS")
        print("=" * 66)
        t = pd.DatetimeIndex(np.asarray(ds["time"].values))
        print(f"  first : {t[0]}")
        print(f"  last  : {t[-1]}")
        print(f"  count : {len(t)}\n")

        # ---------------- attribute completeness ----------------
        print("=" * 66)
        print("ATTRIBUTE COMPLETENESS")
        print("=" * 66)
        missing = [v for v in _EXPECTED_VARS if v not in ds.data_vars]
        extra = [v for v in ds.data_vars if v not in _EXPECTED_VARS]
        print(f"  missing expected: {missing or '(none)'}")
        print(f"  extra/unexpected: {extra or '(none)'}\n")

        # ---------------- value profile ----------------
        print("=" * 66)
        print("VALUE PROFILE")
        print("=" * 66)
        print(
            f"  {'var':<10s} {'%NaN':>7s} {'%zero':>7s} {'%finite!=0':>11s} "
            f"{'min':>12s} {'max':>12s} {'mean':>12s}"
        )
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
            mean = float("nan") if allnan else float(np.nanmean(a))
            print(
                f"  {v:<10s} {100 * nan / total:>6.1f}% "
                f"{100 * zero / total:>6.1f}% "
                f"{100 * nonzero / total:>10.1f}% "
                f"{mn:>12.4g} {mx:>12.4g} {mean:>12.4g}"
            )
        print()

        # ---------------- spot check ----------------
        print("=" * 66)
        print("SPOT CHECK")
        print("=" * 66)
        if args.lat is not None and args.lon is not None and has_latlon:
            latv = np.asarray(ds["latitude"].values)
            lonv = np.asarray(ds["longitude"].values)
            dist = (latv - args.lat) ** 2 + (lonv - args.lon) ** 2
            iy_np, ix_np = np.unravel_index(np.argmin(dist), dist.shape)
            iy, ix = int(iy_np), int(ix_np)
            print(
                f"  cell nearest lat={args.lat}, lon={args.lon} -> "
                f"y={iy}, x={ix} "
                f"(actual lat={latv[iy, ix]:.2f}, lon={lonv[iy, ix]:.2f})"
            )
        else:
            iy, ix = ds.sizes["y"] // 2, ds.sizes["x"] // 2
            print(f"  grid-centre cell y={iy}, x={ix}")
            if not has_latlon:
                print("  (no lat/lon coords in this file -- predates the "
                      "coordinate-retention fix)")

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
    finally:
        ds.close()


if __name__ == "__main__":
    main()
