#!/usr/bin/env python3
"""Spot-check MERRA-2 percentile output files for completeness/sanity.

Percentile files (``merra2_p{10,50,90}_MM_all_attrs.nc``, written by
:mod:`weather.providers.merra2.percentile_index`) carry every canonical
MERRA-2 variable plus a ``source_year`` field recording which historical
year won each grid cell. This reports, per file:

1. **Attribute completeness** — which canonical variables are present.
2. **source_year diversity**  — min/max/unique count, and the winning
   years with the most cells (a healthy run shows real per-cell
   diversity, not one year dominating the whole domain — see
   ``CLAUDE.md``'s MERRA-2 percentile writeup for the expected range).
3. **Value sanity**           — %NaN and min/max/mean for GHI and T.

Usage::

    python inspect_merra2_percentile.py FILE.nc [FILE.nc ...]
    python inspect_merra2_percentile.py \\
        /data/soma/merra2/output/percentile/*.nc
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

#: Canonical variables a MERRA-2 percentile file is expected to carry
#: (see CLAUDE.md's "## cross-provider" naming-unification writeup),
#: plus the percentile-specific `source_year` field. Unlike COSMO-REA6,
#: MERRA-2 does NOT store DNI/DHI (GHI-only; DNI/DHI are reconstructed
#: via pvlib at point-query time -- see point_query.py/dni_pointwise.py)
#: -- do not add them here. QV2M and the 2m/50m wind components are
#: real MERRA-2 fields beyond the minimal cross-provider set.
_EXPECTED_VARS = (
    "T", "GHI", "T_DEW", "ALBEDO", "SNOWFALL", "SNOW_DEPTH",
    "U_10M", "V_10M", "WS_10M", "RH", "PS", "source_year",
    "QV2M", "U_2M", "V_2M", "U_50M", "V_50M",
)


def _check_one(path: Path) -> None:
    import xarray as xr

    with xr.open_dataset(path) as ds:
        print(f"=== {path.name} ===")
        print(f"dims: {dict(ds.sizes)}")

        missing = [v for v in _EXPECTED_VARS if v not in ds.data_vars]
        extra = [v for v in ds.data_vars if v not in _EXPECTED_VARS]
        print(f"missing: {missing or '(none)'}  extra: {extra or '(none)'}")

        sy = ds["source_year"].values
        uniq, counts = np.unique(sy, return_counts=True)
        top5 = dict(
            sorted(
                zip(uniq.tolist(), counts.tolist(), strict=True),
                key=lambda x: -x[1],
            )[:5]
        )
        print(
            f"source_year: min={sy.min()} max={sy.max()} "
            f"unique={len(uniq)}  top cells by count: {top5}"
        )

        for var in ("GHI", "T"):
            a = ds[var].values
            nan_frac = 100.0 * np.isnan(a).sum() / a.size
            print(
                f"{var:<4s}: %NaN={nan_frac:6.2f} "
                f"min={np.nanmin(a):10.3f} "
                f"max={np.nanmax(a):10.3f} "
                f"mean={np.nanmean(a):10.3f}"
            )
        print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("files", nargs="+", help="Percentile .nc file(s) to check")
    args = ap.parse_args()

    for raw in args.files:
        path = Path(raw)
        if not path.exists():
            sys.exit(f"not found: {path}")
        _check_one(path)


if __name__ == "__main__":
    main()
