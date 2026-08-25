#!/usr/bin/env python3
"""Verify a set of MERRA-2 monthly NetCDFs: hours, boundaries, sanity.

Mirrors ``verify_months.py`` (ERA5-Land), adapted for MERRA-2's own
conventions:

1. **Hour count**   — does it hold exactly 24 x days_in_month stamps?
2. **Span**         — MERRA-2's ``tavg1`` collections timestamp each
                      hourly value at the averaging-interval midpoint,
                      so a month runs ``<1st> 00:30 .. <last> 23:30``
                      (NOT ``00:00 .. 23:00`` like ERA5-Land/COSMO — see
                      ``.claude/merra2/merra2_plan.md``, left as-is by
                      design, not a bug).
3. **Continuity**   — does each file start exactly one hour after the
                      previous file ends, across month AND year
                      boundaries (no gap, no overlap, no dupe)?
4. **NaN / min-max**  — per-variable NaN fraction and value range,
                      compared against physically-plausible bounds.
5. **Point profile**  — hourly values of every attribute for one grid
                      cell (default: domain center) over a date range
                      that may span multiple monthly files, to eyeball
                      a diurnal/seasonal profile for sanity.

Usage::

    python verify_merra2_months.py            # uses MERRA-2 output_dir
    python verify_merra2_months.py D:/.../merra2/output
    python verify_merra2_months.py --lat 52.1 --lon 5.2 \\
        --start 2018-02-27 --end 2018-03-03
"""

from __future__ import annotations

import argparse
import calendar
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from weather.providers.merra2.config import get_config

_PATTERN = re.compile(r"MERRA2_(\d{4})_(\d{2})_all_attrs\.nc$")

#: Loose physically-plausible [min, max] per variable, at sea-level-ish
#: European latitudes. Values outside these bounds are flagged, not
#: necessarily wrong (e.g. a genuine storm PS low) — a sanity net, not
#: a hard spec.
#: Both the pre-2026-07-26 raw names (T2M, U10M/V10M, U2M/V2M) and the
#: cross-provider canonical names they were renamed to (T; U_10M/V_10M;
#: U_2M/V_2M) are listed below. The already-completed 46-year archive on
#: disk still has the OLD names (renames only take effect on the NEXT
#: MERRA-2 rerun -- same backward-compatible pattern already used for
#: T2M -> T, see .claude/open.md); having both present means this table
#: keeps working unmodified either way, whichever this tool is pointed at.
_PLAUSIBLE_RANGE: dict[str, tuple[float, float]] = {
    "ALBEDO": (0.0, 1.0),
    "PS": (85000.0, 108000.0),  # Pa; ERA5-Land-comparable surface range
    "QV2M": (0.0, 0.030),  # kg/kg
    # degC (already converted). Widened from (-40, 45) after the full
    # 1980-2025 run: the Europe box spans 34N (Saharan margin) to 72N
    # (Arctic Scandinavia/Kola), so -51.09/51.72 over 46 years is real
    # climate, not corrupted data -- same rationale as the PS/Alps case.
    "T2M": (-55.0, 55.0),
    "T": (-55.0, 55.0),
    "U10M": (-45.0, 45.0),  # m/s
    "U_10M": (-45.0, 45.0),
    "U2M": (-45.0, 45.0),
    "U_2M": (-45.0, 45.0),
    "U50M": (-50.0, 50.0),  # hub-height, slightly stronger than 10m
    "U_50M": (-50.0, 50.0),
    "V10M": (-45.0, 45.0),
    "V_10M": (-45.0, 45.0),
    "V2M": (-45.0, 45.0),
    "V_2M": (-45.0, 45.0),
    "V50M": (-50.0, 50.0),
    "V_50M": (-50.0, 50.0),
    # m; real 46-year max observed was 4.78 m (see merra2_plan.md).
    "SNODP": (0.0, 10.0),
    "SNOW_DEPTH": (0.0, 10.0),
    # kg/m^2/h; real 46-year max observed was 76.9 (see merra2_plan.md).
    "PRECSNOLAND": (0.0, 150.0),
    "SNOWFALL": (0.0, 150.0),
    "GHI": (0.0, 1400.0),  # W/m^2; solar constant ~1361
    "WS_10M": (0.0, 60.0),  # m/s
    "RH": (0.0, 100.0),  # %
}


def _find_files(out_dir: Path) -> list[tuple[int, int, Path]]:
    files = []
    for p in out_dir.glob("MERRA2_*_all_attrs.nc"):
        m = _PATTERN.search(p.name)
        if m:
            files.append((int(m.group(1)), int(m.group(2)), p))
    files.sort(key=lambda r: (r[0], r[1]))
    return files


def main() -> None:  # noqa: C901 - straight-line verification report
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "output_dir", nargs="?", default=None,
        help=(
            "Folder of monthly MERRA2_*_all_attrs.nc files. "
            "Default: <MERRA_WORK_DIR>/output."
        ),
    )
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument(
        "--start", default=None, metavar="YYYY-MM-DD",
        help="Profile start date (inclusive). Requires --end.",
    )
    ap.add_argument(
        "--end", default=None, metavar="YYYY-MM-DD",
        help="Profile end date (inclusive). Requires --start.",
    )
    args = ap.parse_args()

    out_dir = (
        Path(args.output_dir) if args.output_dir is not None
        else get_config()["output_dir"]
    )
    if not out_dir.is_dir():
        sys.exit(f"not a directory: {out_dir}")

    import xarray as xr

    files = _find_files(out_dir)
    if not files:
        sys.exit("No monthly files found.")

    print(f"Found {len(files)} monthly file(s) in {out_dir}\n")

    # ---------------- 1-3: hour count, span, continuity ----------------
    print("=" * 78)
    print(f"{'file':<10s} {'stamps':>7s} {'expect':>7s} {'first stamp':<20s} "
          f"{'last stamp':<20s} {'ok':>4s}")
    print("=" * 78)

    prev_last = None
    prev_label = None
    rows: list[tuple[int, int, Path]] = []
    any_fail = False

    for yr, mo, path in files:
        ds = xr.open_dataset(path)
        try:
            t = pd.DatetimeIndex(np.asarray(ds["time"].values))
            n = len(t)
            ndays = calendar.monthrange(yr, mo)[1]
            expect = 24 * ndays

            want_first = pd.Timestamp(yr, mo, 1, 0, 30)
            want_last = pd.Timestamp(yr, mo, ndays, 23, 30)
            steps_ok = bool(
                n > 1
                and (np.diff(t.values).astype("timedelta64[m]") ==
                     np.timedelta64(60, "m")).all()
            )
            ok = (
                n == expect and t[0] == want_first and t[-1] == want_last
                and steps_ok
            )
            any_fail = any_fail or not ok

            print(
                f"{yr}-{mo:02d}     {n:>7d} {expect:>7d} "
                f"{str(t[0]):<20s} {str(t[-1]):<20s} "
                f"{'OK' if ok else 'FAIL':>4s}"
            )

            if prev_last is not None:
                delta = (t[0] - prev_last).total_seconds() / 3600.0
                if delta != 1.0:
                    any_fail = True
                    print(
                        f"    !! discontinuity: {prev_label} ends "
                        f"{prev_last}, {yr}-{mo:02d} starts {t[0]} "
                        f"(gap {delta:.0f} h; expected 1 h)"
                    )
            prev_last = t[-1]
            prev_label = f"{yr}-{mo:02d}"
            rows.append((yr, mo, path))
        finally:
            ds.close()

    # ---------------- 4: NaN / min-max / plausibility, whole year -------
    print()
    print("=" * 88)
    print("NaN / MIN-MAX / PLAUSIBILITY (aggregated across all files)")
    print("=" * 88)
    print(f"  {'var':<8s} {'%NaN':>7s} {'min':>12s} {'max':>12s} "
          f"{'plausible range':<20s} {'verdict':<10s}")

    agg_min: dict[str, float] = {}
    agg_max: dict[str, float] = {}
    agg_nan_frac: dict[str, list[float]] = {}

    for _, _, path in rows:
        ds = xr.open_dataset(path)
        try:
            for v in ds.data_vars:
                v = str(v)
                a = ds[v].values
                if a.ndim < 3:
                    continue
                nan_frac = float(np.isnan(a).mean())
                agg_nan_frac.setdefault(v, []).append(nan_frac)
                if not np.all(np.isnan(a)):
                    mn = float(np.nanmin(a))
                    mx = float(np.nanmax(a))
                    agg_min[v] = min(mn, agg_min.get(v, mn))
                    agg_max[v] = max(mx, agg_max.get(v, mx))
        finally:
            ds.close()

    for v in sorted(agg_min):
        nan_pct = 100.0 * (sum(agg_nan_frac[v]) / len(agg_nan_frac[v]))
        lo, hi = _PLAUSIBLE_RANGE.get(v, (float("-inf"), float("inf")))
        in_range = lo <= agg_min[v] and agg_max[v] <= hi
        verdict = "OK" if in_range else "OUT-OF-RANGE"
        if v not in _PLAUSIBLE_RANGE:
            verdict = "(no range set)"
        print(
            f"  {v:<8s} {nan_pct:>6.1f}% {agg_min[v]:>12.4g} "
            f"{agg_max[v]:>12.4g} {f'[{lo:g}, {hi:g}]':<20s} {verdict:<10s}"
        )

    # ---------------- 5: point profile over a (possibly cross-month) span
    print()
    print("=" * 88)
    print("POINT PROFILE")
    print("=" * 88)

    ds0 = xr.open_dataset(rows[0][2])
    try:
        latv = np.asarray(ds0["latitude"].values)
        lonv = np.asarray(ds0["longitude"].values)
    finally:
        ds0.close()

    lat = args.lat if args.lat is not None else float(latv[len(latv) // 2])
    lon = args.lon if args.lon is not None else float(lonv[len(lonv) // 2])
    lon_t = lon % 360.0 if lonv.max() > 180.0 else lon
    iy = int(np.abs(latv - lat).argmin())
    ix = int(np.abs(lonv - lon_t).argmin())
    print(f"  cell: lat={latv[iy]:.3f} lon={lonv[ix]:.3f} (iy={iy}, ix={ix})")

    if args.start and args.end:
        start = pd.Timestamp(args.start)
        end = pd.Timestamp(args.end) + pd.Timedelta(hours=23, minutes=59)
        needed = [
            (yr, mo, path) for yr, mo, path in rows
            if pd.Timestamp(yr, mo, 1) <= end
            and pd.Timestamp(yr, mo, calendar.monthrange(yr, mo)[1]) >= start
        ]
        if not needed:
            print(f"  no files cover {args.start} .. {args.end}")
        else:
            dsets = [xr.open_dataset(p) for _, _, p in needed]
            combined = xr.concat(dsets, dim="time").sel(
                time=slice(start, end),
            )
            print(f"  range: {args.start} .. {args.end}  "
                  f"({combined.sizes['time']} hourly steps)")
            print()
            for v in combined.data_vars:
                if combined[v].ndim < 1:
                    continue
                vals = combined[v].isel(y=iy, x=ix).values
                print(f"  {v:<8s}: " + np.array2string(
                    vals, precision=2, max_line_width=200, threshold=48,
                    formatter={"float_kind": lambda x: f"{x:8.2f}"},
                ))
            for d in dsets:
                d.close()
    else:
        print("  (pass --start/--end to print an hourly profile, e.g.")
        print("   --start 2018-02-27 --end 2018-03-03 to span Feb->Mar)")

    print()
    if any_fail:
        sys.exit("FAIL: one or more months had a stamp-count/continuity issue")
    print("All months OK: correct hour counts, HH:30 span, no gaps.")


if __name__ == "__main__":
    main()
