"""Repair physically impossible flux spikes in ERA5-Land output.

Companion to :mod:`~weather.providers.era5_land.boundary_repair`, which
fixes the *first* stamp of every month. This module fixes a rarer,
unrelated defect found at interior stamps.

The defect
----------
ERA5-Land accumulations reset at 00:00 daily, so the hourly flux at
stamp ``HH:00`` is ``(acc[step] - acc[step - 1]) / 3600`` with
``acc[0] = 0``. For a very small number of individual grid cells the
transform lost ``acc[step]`` for one stamp and stored the *previous
day's whole accumulated total* in its place, producing an hourly "flux"
of thousands of W/m^2 -- several times the solar constant.

Two stamps are affected per occurrence:

* stamp ``i`` holds the raw accumulated total instead of its increment;
* stamp ``i + 1`` was then differenced against a missing ``acc[step]``
  (treated as zero), so it is too large by ``acc[step] / 3600``.

Measured incidence on the real 1950-2025 archive: 11 values across
912 monthly files -- 11 out of roughly 1.1e11 -- each a single cell at
a single stamp, all at high northern latitudes in summer.

The repair
----------
Unlike the month-boundary repair, the correct value is *not* derivable
from data already in the NetCDF: it was never written. This module
re-reads the two accumulation steps it needs from the cached raw GRIB
and recomputes both stamps exactly. No interpolation or gap-filling is
involved.

Where the raw accumulation is itself absent -- ERA5-Land flags absent
data with a sentinel value (``missingValue``, 9999) rather than a NaN
bit pattern -- the hourly value is genuinely unknowable and is blanked
to NaN. Decoding that sentinel matters: taken at face value it reads as
a perfectly plausible 9999 J/m^2 (2.8 W/m^2), so an undecoded repair
would replace a visibly absurd number with an invisibly wrong one. On
the real archive every one of the 11 occurrences is this case: the
step-1 field carries exactly one missing cell beyond the standard
land-sea mask, and it is the spiking cell.

Auditability
------------
Every correction is recorded verbatim in the file's ``spike_repair``
global attribute (JSON), so a repaired file always says what was
changed, from what, and to what. Files with nothing to fix are left
untouched -- including their mtime.

Run standalone::

    python -m weather.providers.era5_land.spike_repair \\
        --output-dir /data/soma/era5_land/output \\
        --download-dir /data/soma/era5_land/download
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from eccodes import (
    codes_get,
    codes_get_double_array,
    codes_grib_new_from_file,
    codes_release,
)

logger = logging.getLogger("spike_repair")

_PATTERN = re.compile(r"ERA5_LAND_(\d{4})_(\d{2})_all_attrs\.nc$")

#: Physical ceiling for an hourly mean irradiance, W/m^2. The solar
#: constant is ~1361 W/m^2 at the top of the atmosphere; a surface
#: hourly mean cannot approach it, so anything above this is corrupt
#: rather than merely extreme.
CEILING_W_M2 = 1500.0

#: Output variable -> GRIB shortName of the accumulation it derives from.
_ACCUMULATED = {"GHI": "ssrd"}

#: Seconds per accumulation step (hourly data).
_STEP_SECONDS = 3600.0

#: Written into the ``spike_repair`` global attribute.
_DONE_FLAG = "SPIKE_REPAIRED"


def _index_to_step(stamp) -> tuple[int, int]:
    """Map an output timestamp to its GRIB ``(dataDate, step)``.

    ERA5-Land packs each day as steps 1..24 counted from that day's
    00:00, so the stamp at ``HH:00`` is step ``HH`` of the same date --
    except midnight, which is step 24 of the *previous* date.

    Parameters
    ----------
    stamp : pandas.Timestamp
        Output time coordinate value.

    Returns
    -------
    tuple of (int, int)
        ``(dataDate as YYYYMMDD, step)``.
    """
    ts = pd.Timestamp(stamp)
    if ts.hour == 0:
        prev = ts - pd.Timedelta(days=1)
        return int(prev.strftime("%Y%m%d")), 24
    return int(ts.strftime("%Y%m%d")), ts.hour


def find_spikes(
    ds, var: str, ceiling: float = CEILING_W_M2
) -> list[tuple[int, int, int, float]]:
    """Locate physically impossible values of *var*.

    Stamp 0 is excluded: its cross-month repair belongs to
    :mod:`boundary_repair`, and an unrepaired first stamp is expected to
    be large rather than corrupt.

    Parameters
    ----------
    ds : xarray.Dataset
        An opened monthly output file.
    var : str
        Variable to scan.
    ceiling : float, optional
        Values strictly above this are treated as corrupt.

    Returns
    -------
    list of tuple
        ``(time_index, y_index, x_index, value)``, ascending by stamp.
    """
    if var not in ds.data_vars:
        return []
    data = ds[var].values
    hits = np.argwhere(np.nan_to_num(data, nan=0.0) > ceiling)
    return [
        (int(t), int(y), int(x), float(data[t, y, x]))
        for t, y, x in hits
        if int(t) >= 1
    ]


def _has_spike(nc_path: Path, ceiling: float = CEILING_W_M2) -> bool:
    """True if *nc_path* holds any impossible value. Worker for the sweep.

    Parameters
    ----------
    nc_path : pathlib.Path
        Monthly output NetCDF to inspect.
    ceiling : float, optional
        Detection threshold in W/m^2.

    Returns
    -------
    bool
        Whether at least one interior stamp exceeds *ceiling*.
    """
    try:
        with xr.open_dataset(nc_path, engine="netcdf4") as ds:
            return any(
                find_spikes(ds, var, ceiling) for var in _ACCUMULATED
            )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not be scanned", nc_path.name)
        return False


def _read_accumulations(
    grib_path: Path, shortname: str, wanted: set[tuple[int, int]]
) -> dict[tuple[int, int], np.ndarray]:
    """Read specific ``(dataDate, step)`` accumulation fields from GRIB.

    Parameters
    ----------
    grib_path : pathlib.Path
        Raw monthly GRIB.
    shortname : str
        GRIB ``shortName`` of the accumulated field (e.g. ``"ssrd"``).
    wanted : set of tuple
        ``(dataDate, step)`` pairs to collect.

    Returns
    -------
    dict
        Mapping of each requested pair to its flat value array. Pairs
        absent from the file are simply missing from the result.
    """
    out: dict[tuple[int, int], np.ndarray] = {}
    if not wanted:
        return out
    with open(grib_path, "rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if codes_get(gid, "shortName") == shortname:
                    key = (
                        int(codes_get(gid, "dataDate")),
                        int(codes_get(gid, "step")),
                    )
                    if key in wanted and key not in out:
                        vals = np.asarray(
                            codes_get_double_array(gid, "values")
                        )
                        # ERA5-Land marks absent data with a sentinel
                        # (9999) rather than a NaN bit pattern. Decode
                        # it, or a missing cell reads as a plausible
                        # 9999 J/m^2 -> 2.8 W/m^2 and we would be
                        # inventing data out of a flag.
                        missing = float(codes_get(gid, "missingValue"))
                        vals[vals == missing] = np.nan
                        out[key] = vals
            finally:
                codes_release(gid)
            if len(out) == len(wanted):
                break
    return out


def repair_file(
    nc_path: Path,
    grib_path: Path,
    *,
    ceiling: float = CEILING_W_M2,
    dry_run: bool = False,
) -> list[dict]:
    """Repair every impossible value in one monthly output file.

    Parameters
    ----------
    nc_path : pathlib.Path
        Monthly output NetCDF to inspect and, if needed, rewrite.
    grib_path : pathlib.Path
        The matching cached raw GRIB, source of the true values.
    ceiling : float, optional
        Detection threshold in W/m^2.
    dry_run : bool, optional
        Report what would change without writing.

    Returns
    -------
    list of dict
        One record per corrected value; empty if the file was clean.
    """
    with xr.open_dataset(nc_path, engine="netcdf4") as probe:
        spikes = {
            var: find_spikes(probe, var, ceiling)
            for var in _ACCUMULATED
        }
        times = pd.to_datetime(probe["time"].values)
        n_x = int(probe.sizes["x"])

    if not any(spikes.values()):
        return []

    if not grib_path.exists():
        logger.error(
            "%s: %d spike(s) found but raw GRIB %s is missing -- cannot "
            "recover the true value, leaving the file untouched",
            nc_path.name,
            sum(len(v) for v in spikes.values()),
            grib_path.name,
        )
        return []

    # A corrupt stamp also breaks the NEXT stamp's difference, so both
    # are recomputed. Collect every accumulation step that needs.
    todo: list[tuple[str, int, int, int]] = []
    wanted: set[tuple[int, int]] = set()
    for var, hits in spikes.items():
        for t_idx, y_idx, x_idx, _ in hits:
            for idx in (t_idx, t_idx + 1):
                if idx >= len(times):
                    continue
                todo.append((var, idx, y_idx, x_idx))
                date, step = _index_to_step(times[idx])
                wanted.add((date, step))
                if step > 1:
                    wanted.add((date, step - 1))

    fields: dict[str, dict] = {}
    for var in {v for v, *_ in todo}:
        fields[var] = _read_accumulations(
            grib_path, _ACCUMULATED[var], wanted
        )

    records: list[dict] = []
    ds = xr.open_dataset(nc_path, engine="netcdf4").load()
    try:
        for var, t_idx, y_idx, x_idx in todo:
            date, step = _index_to_step(times[t_idx])
            acc = fields[var].get((date, step))
            if acc is None:
                logger.error(
                    "%s: GRIB has no %s message for %d step %d",
                    nc_path.name, _ACCUMULATED[var], date, step,
                )
                continue
            prev = 0.0
            if step > 1:
                prev_acc = fields[var].get((date, step - 1))
                if prev_acc is None:
                    logger.error(
                        "%s: GRIB has no %s predecessor for %d step %d",
                        nc_path.name, _ACCUMULATED[var], date, step,
                    )
                    continue
                prev = float(prev_acc[y_idx * n_x + x_idx])
            truth = (
                float(acc[y_idx * n_x + x_idx]) - prev
            ) / _STEP_SECONDS

            before = float(ds[var].values[t_idx, y_idx, x_idx])
            if np.isclose(before, truth, rtol=1e-6, atol=1e-6):
                continue
            if not dry_run:
                ds[var].values[t_idx, y_idx, x_idx] = np.float32(truth)
            records.append({
                "var": var,
                "time": str(times[t_idx]),
                "time_index": t_idx,
                "y": y_idx,
                "x": x_idx,
                "from": round(before, 4),
                # None, not NaN: the accumulation this stamp needs is
                # absent upstream, so the hourly value is genuinely
                # unknowable and is blanked rather than invented.
                "to": None if not np.isfinite(truth) else round(truth, 4),
            })

        if records and not dry_run:
            note = json.loads(ds.attrs.get("spike_repair", "{}") or "{}")
            note.setdefault("status", _DONE_FLAG)
            note.setdefault("corrections", [])
            note["corrections"].extend(records)
            note["source"] = grib_path.name
            ds.attrs["spike_repair"] = json.dumps(note)
            enc = {
                v: {"zlib": True, "complevel": 1, "dtype": "float32"}
                for v in ds.data_vars
            }
            tmp = nc_path.with_suffix(".nc.tmp")
            ds.to_netcdf(
                tmp, encoding=enc, format="NETCDF4", engine="netcdf4"
            )
            ds.close()
            tmp.replace(nc_path)
    finally:
        ds.close()

    return records


def repair_spikes(
    output_dir: Path,
    download_dir: Path,
    *,
    ceiling: float = CEILING_W_M2,
    dry_run: bool = False,
    workers: int = 16,
) -> dict[str, list[dict]]:
    """Scan an output folder and repair every impossible value found.

    Parameters
    ----------
    output_dir : pathlib.Path
        Folder of monthly ``ERA5_LAND_*_all_attrs.nc`` files.
    download_dir : pathlib.Path
        Folder of the matching cached ``.grib`` files.
    ceiling : float, optional
        Detection threshold in W/m^2.
    dry_run : bool, optional
        Report without writing.
    workers : int, optional
        Parallelism for the detection sweep.

    Returns
    -------
    dict
        Mapping of filename to its correction records, for files that
        needed at least one.
    """
    files = sorted(
        p for p in output_dir.glob("ERA5_LAND_*_all_attrs.nc")
        if _PATTERN.search(p.name)
    )
    logger.info("Scanning %d file(s) for values > %.0f W/m^2",
                len(files), ceiling)

    # Detection reads every hour of every file, so it dominates the
    # runtime (~1.2 GB per file) even though only a handful of files
    # ever need repairing. Fan it out, then repair the few hits
    # sequentially -- those also need the raw GRIB.
    with ProcessPoolExecutor(max_workers=workers) as pool:
        flagged = [
            path
            for path, hit in zip(
                files,
                pool.map(
                    partial(_has_spike, ceiling=ceiling),
                    files,
                    chunksize=4,
                ),
                strict=True,
            )
            if hit
        ]
    logger.info("%d file(s) contain impossible values", len(flagged))

    fixed: dict[str, list[dict]] = {}
    for nc_path in flagged:
        grib_path = download_dir / (nc_path.stem + ".grib")
        records = repair_file(
            nc_path, grib_path, ceiling=ceiling, dry_run=dry_run
        )
        if records:
            fixed[nc_path.name] = records
            for rec in records:
                target = rec["to"]
                logger.info(
                    "%s %s %s (y=%d,x=%d): %.1f -> %s",
                    nc_path.name, rec["var"], rec["time"],
                    rec["y"], rec["x"], rec["from"],
                    "NaN (absent upstream)" if target is None
                    else f"{target:.3f} W/m^2",
                )

    logger.info(
        "%s: %d file(s) repaired, %d value(s) corrected",
        "DRY RUN" if dry_run else "Done",
        len(fixed), sum(len(v) for v in fixed.values()),
    )
    return fixed


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Repair impossible flux spikes in ERA5-Land output",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--download-dir", required=True, type=Path)
    parser.add_argument("--ceiling", type=float, default=CEILING_W_M2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    repair_spikes(
        args.output_dir,
        args.download_dir,
        ceiling=args.ceiling,
        dry_run=args.dry_run,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
