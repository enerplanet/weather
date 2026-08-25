"""ERA5-Land GRIB → analysis-ready monthly dataset.

Reads the monthly all-variables GRIB produced by
:mod:`~weather.providers.era5_land.download`, applies unit conversions
(see :mod:`~weather.providers.era5_land.downloaded_attributes`),
de-accumulates the radiation flux, computes the Spencer (1971) solar
zenith angle, night-masks GHI, and returns a single
:class:`xarray.Dataset` ready for export.

Design goals
------------
* **Consistency with COSMO-REA6.**  The stored ``GHI`` is an *hourly
  mean flux* in W/m², identical in semantics to COSMO's instantaneous
  GHI, so downstream code (``common.percentile_index`` daily-sum,
  ``common.merge``) works unchanged.  Night-masking uses the same
  Spencer SZA + ``mask_night`` machinery as COSMO.
* **DNI/DHI are NOT precomputed.**  ERA5-Land supplies only total
  shortwave (``ssrd``); any direct/diffuse split is a *model estimate*.
  Baking one decomposition choice into the archive would be slow and
  would hide its uncertainty.  GHI is stored; users derive DNI/DHI at
  point of use via :mod:`~weather.providers.era5_land.dni_pointwise`.
* **Vectorised + dask-chunked.**  All math broadcasts over
  ``(time, latitude, longitude)`` and is chunked along time so peak RAM
  stays bounded.  No per-cell Python loops.

The critical step: ssrd de-accumulation
----------------------------------------
ERA5-Land ``ssrd`` is **accumulated** (GRIB stepType ``accm``), reset
at 00 UTC each day.  The value at hour *H* is cumulative J/m² since the
daily reset, *not* the energy in that single hour.  Naively doing
``ssrd / 3600`` would store a daily ramp and break any daily-sum
diagnostic.  We de-accumulate to a true per-hour flux::

    flux[h] = (ssrd[h] - ssrd[h-1]) / 3600     within each UTC day
    flux[first hour of day] = ssrd[first] / 3600

Because the accumulation window is anchored daily, every UTC day
de-accumulates self-consistently with no cross-month dependency.  A
non-negativity assertion guards against corrupt input (a negative
de-accumulated flux indicates a malformed or mis-ordered file).

.. note::
   This assumes the CDS response carries the standard daily-reset
   accumulation.  If you switch ``data_format`` or dataset, verify the
   stepType before trusting the de-accumulation.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Hashable
from typing import Any, cast

import cfgrib  # type: ignore
import numpy as np
import pandas as pd
import xarray as xr

from ...common.cf_conventions import attach_cf_latlon_attrs
from ...common.derived_attributes import magnus_rh, mask_night, wind_speed
from ...common.solar_position import spencer_zenith
from .downloaded_attributes import ATTRIBUTES

logger = logging.getLogger(__name__)

#: cfgrib/eccodes short names for each configured CDS variable.  Used to
#: locate variables after cfgrib decode regardless of the long CDS name.
#: Derived from ``downloaded_attributes.ATTRIBUTES[*]["e5l_name"]``.
SHORT_NAMES: dict[str, str] = {
    cds_name: meta["e5l_name"] for cds_name, meta in ATTRIBUTES.items()
}

#: Default time chunk (hours) for dask — ~1 week, matching merge.py.
_TIME_CHUNK: int = 168

#: GRIB accumulated-radiation variable (Global Horizontal Irradiance).
_SSRD: str = "ssrd"


# ---------------------------------------------------------------------------
# Unit conversions (mirrors downloaded_attributes.ATTRIBUTES conversions)
# ---------------------------------------------------------------------------

def _convert_units(ds: xr.Dataset) -> xr.Dataset:
    """Apply the unit conversions declared in ``downloaded_attributes``.

    Only conversions that are pure element-wise scalings are applied
    here (temperature K→°C, snowfall m→kg/m²/h).  ``ssrd`` is handled
    separately by :func:`_deaccumulate_to_ghi` because it also requires
    de-accumulation.  Wind, pressure, albedo, and snow depth pass
    through unchanged (already in target units, or require external
    density data we do not assume).
    """
    out = ds

    # Kelvin -> Celsius for 2 m temperature and dew point.
    for short in ("t2m", "d2m"):
        if short in out:
            out[short] = out[short] - 273.15
            out[short].attrs["units"] = "degC"

    # snowfall: by the time _convert_units runs, sf has ALREADY been
    # de-accumulated along the forecast step (see _deaccumulate_along_step),
    # so it holds the per-hour increment in metres of water equivalent.
    # Multiply by 1000 to get an hourly-rate mass flux kg/m^2/h (1 m water
    # equiv over 1 m^2 = 1000 kg).
    if "sf" in out:
        out["sf"] = (out["sf"] * 1000.0).clip(min=0.0)
        out["sf"].attrs["units"] = "kg/m^2/h"
        out["sf"].attrs["long_name"] = "Snowfall rate (water equivalent)"
        out["sf"].attrs["note"] = (
            "de-accumulated along forecast step; hourly rate"
        )

    return out


# ---------------------------------------------------------------------------
# Relative humidity from 2 m temperature and dew point (Magnus formula)
# ---------------------------------------------------------------------------

def _compute_rh(ds_raw: xr.Dataset) -> xr.DataArray | None:
    """Relative humidity (%) from raw-Kelvin ``t2m`` and ``d2m``.

    Uses the August-Roche-Magnus approximation::

        RH = 100 * exp( a*Td/(b+Td) - a*T/(b+T) )

    with ``a = 17.625``, ``b = 243.04`` and T, Td in °C.

    IMPORTANT: call this with the dataset **before** ``_convert_units``
    has run, i.e. while ``t2m``/``d2m`` are still in Kelvin.  Computing
    it after conversion would subtract 273.15 a second time.

    Returns
    -------
    xarray.DataArray or None
        RH in percent, clipped to [0, 100]; ``None`` if either input
        variable is absent.
    """
    if "t2m" not in ds_raw or "d2m" not in ds_raw:
        logger.warning(
            "t2m and/or d2m missing; skipping RH (have %s)",
            list(ds_raw.data_vars),
        )
        return None

    t = ds_raw["t2m"] - 273.15      # °C
    td = ds_raw["d2m"] - 273.15     # °C

    # Formula is derived_attributes.magnus_rh -- single source of
    # truth, also used by derived_attributes._era5_rh.
    rh = cast(xr.DataArray, magnus_rh(t, td))
    rh.name = "RH"
    rh.attrs = {
        "long_name": "Relative Humidity",
        "units": "%",
        "description": (
            "Magnus formula (a=17.625, b=243.04) from 2 m temperature "
            "and 2 m dew point"
        ),
        "derivation": "RH = 100*exp(a*Td/(b+Td) - a*T/(b+T))",
    }
    return rh


def _compute_wind_speed(ds: xr.Dataset) -> xr.DataArray | None:
    """Scalar 10 m wind speed ``WS_10M = sqrt(u10**2 + v10**2)``.

    The magnitude is invariant under the lat/lon vs rotated-pole frame,
    so no vector rotation is needed (same reasoning as COSMO-REA6).

    Returns
    -------
    xarray.DataArray or None
        Wind speed in m/s, or ``None`` if either component is absent.
    """
    if "u10" not in ds or "v10" not in ds:
        logger.warning(
            "u10 and/or v10 missing; skipping WS_10M (have %s)",
            list(ds.data_vars),
        )
        return None

    # Formula is derived_attributes.wind_speed -- single source of
    # truth, shared with every provider.
    ws = cast(xr.DataArray, wind_speed(ds["u10"], ds["v10"]))
    ws.name = "WS_10M"
    ws.attrs = {
        "long_name": "Wind speed at 10 m",
        "units": "m/s",
        "description": "scalar wind speed sqrt(u10^2 + v10^2)",
        "derivation": "WS_10M = sqrt(u10**2 + v10**2)",
    }
    return ws


#: ERA5-Land accumulated fields (GRIB stepType 'accm') that must be
#: differenced along 'step' within each forecast day before flattening.
_ACCUMULATED_VARS: frozenset = frozenset({"ssrd", "sf"})


def _deaccumulate_along_step(ds: xr.Dataset) -> xr.Dataset:
    """Difference accumulated vars along ``step`` within each forecast day.

    For a cube on ``(time=forecast-day, step=hour)``, an accumulated
    field at step *s* is the cumulative total since the day's reset.  The
    per-hour increment is ``a[s] - a[s-1]`` for s>=1, and ``a[0]`` for the
    first step.  This is unambiguous on the step axis (no cross-day
    boundary issues), so we do it BEFORE flattening to a 1-D time axis.

    Variables not in :data:`_ACCUMULATED_VARS` are left untouched.
    """
    if "step" not in ds.dims:
        return ds

    first_step = ds["step"].isel(step=0)
    out = ds
    for var in list(out.data_vars):
        if var not in _ACCUMULATED_VARS:
            continue
        a = out[var]
        prev = a.shift(step=1)
        # Three cases per (forecast-day, step):
        #   * step == first step coordinate  -> keep a (daily reset: the
        #     value IS its own increment);
        #   * predecessor present            -> difference a - prev;
        #   * predecessor MISSING (NaN) with a finite value -> KEEP RAW.
        #     This happens for exactly one message per monthly file: the
        #     previous month's last forecast day arrives with step 24
        #     only (no step 23).  Keeping the raw accumulated value there
        #     (instead of NaN) lets boundary_repair.py compute the true
        #     increment later from the previous month's file:
        #         increment = raw_first - sum(prev month's last-day
        #                                     increments 01:00..23:00)
        #     Ocean cells stay NaN throughout (a itself is NaN there).
        deaccum = xr.where(
            a["step"] == first_step,
            a,
            xr.where(prev.notnull(), a - prev, a),
        )
        # xr.where can reorder dims (condition dim goes first); restore.
        deaccum = deaccum.transpose(*a.dims)
        deaccum.attrs = dict(a.attrs)
        deaccum.attrs["note"] = (
            "de-accumulated along forecast step (per-hour increment); "
            "the file's FIRST timestamp keeps the RAW accumulated value "
            "(its predecessor lives in the previous month's file) — "
            "boundary_repair.py fixes this automatically (run_pipeline's "
            "STEP 3/3)"
        )
        out[var] = deaccum
    return out


def _deaccumulate_to_ghi(ds: xr.Dataset) -> xr.DataArray:
    """Convert per-hour ``ssrd`` energy (J/m²) to mean GHI flux (W/m²).

    By the time this runs, ``ssrd`` has ALREADY been de-accumulated along
    the forecast step (see :func:`_deaccumulate_along_step`), so it holds
    the per-hour energy increment.  Here we only divide by 3600 s and
    guard against negatives.

    Raises
    ------
    KeyError
        If ``ssrd`` is absent from *ds*.
    RuntimeError
        If de-accumulation produces physically impossible negative
        flux beyond a small numerical tolerance (corrupt input).
    """

    if _SSRD not in ds:
        raise KeyError(
            f"{_SSRD!r} not found in dataset; cannot derive GHI. "
            f"Present: {list(ds.data_vars)}"
        )

    ssrd = ds[_SSRD]
    if "time" not in ssrd.dims:
        raise RuntimeError("ssrd has no 'time' dimension")

    # ssrd already holds per-hour energy (J/m^2) after
    # _deaccumulate_along_step; convert to mean flux over the hour.
    flux = ssrd / 3600.0

    # Numerical-safety / corruption guard.  Tiny negatives from float
    # rounding are clipped; large negatives indicate bad input.
    min_val = float(flux.min().compute()) if hasattr(
        flux.data, "dask"
    ) else float(flux.min())
    if min_val < -1.0:  # W/m^2 tolerance
        raise RuntimeError(
            "ssrd per-hour flux < -1 W/m^2 "
            f"(min={min_val:.3f}); de-accumulation or source file "
            "problem (check forecast step ordering)."
        )
    flux = flux.clip(min=0.0)

    flux.name = "GHI"
    flux.attrs = {
        "long_name": "Global Horizontal Irradiance",
        "units": "W/m^2",
        "description": (
            "ssrd de-accumulated along forecast step and divided by "
            "3600 s; hourly-mean flux (ocean = NaN)"
        ),
        "derivation": "ERA5_LAND GHI = deaccum_step(ssrd) / 3600",
    }
    return flux


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _lat_lon_arrays(ds: xr.Dataset) -> tuple[np.ndarray, np.ndarray]:
    """Return broadcastable (lat, lon) arrays from an ERA5-Land dataset.

    ERA5-Land is on a regular lat/lon grid, so 1-D coordinate vectors
    are meshed into 2-D ``(y, x)`` arrays for per-cell zenith.
    """
    lat_name = "latitude" if "latitude" in ds.coords else "lat"
    lon_name = "longitude" if "longitude" in ds.coords else "lon"
    lat1d = ds[lat_name].values
    lon1d = ds[lon_name].values
    lat2d, lon2d = np.meshgrid(lat1d, lon1d, indexing="ij")
    return lat2d, lon2d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _normalize_longitude(ds: xr.Dataset) -> xr.Dataset:
    """Put longitude on a single -180..180 convention and sort ascending.

    ERA5-Land cubes can mix longitude conventions: most variables come on
    -180..180 (e.g. -11..32 for a Europe crop) while an edition-2 cube
    (e.g. snow depth 'sde') may arrive on 0..360 (e.g. 349..32, i.e.
    -11 wrapped to 349).  These are the SAME physical points but different
    labels, so an outer-join merge treats them as disjoint and NaN-fills
    everything.  Converting all cubes to -180..180 and sorting makes the
    longitude axes identical so the merge aligns cleanly.
    """

    lon_name = next(
        (c for c in ("longitude", "lon") if c in ds.coords), None
    )
    if lon_name is None:
        return ds
    lon = ds[lon_name].values
    if lon.max() > 180.0:
        new_lon = ((lon + 180.0) % 360.0) - 180.0
        ds = ds.assign_coords({lon_name: new_lon})
        ds = ds.sortby(lon_name)
    return ds


def _flatten_to_hourly(ds: xr.Dataset) -> xr.Dataset:
    """Collapse a ``(time, step)`` ERA5-Land cube to a 1-D hourly axis.

    ERA5-Land forecast cubes arrive as ``(time=forecast-day,
    step=hour)``.  The true timestamp of each element is the
    ``valid_time`` 2-D coordinate.  This stacks ``(time, step)`` into a
    single dimension, replaces it with ``valid_time``, sorts, and drops
    duplicate timestamps (forecast overlaps).

    Cubes already on a flat hourly ``time`` axis are returned unchanged
    (apart from dropping scalar ``step``/``surface`` coords).
    """

    # Already flat hourly: just tidy scalar coords.
    if "step" not in ds.dims:
        drop = [
            c for c in ("step", "surface", "number")
            if c in ds.coords and ds[c].ndim == 0
        ]
        out = ds.drop_vars(drop, errors="ignore")
        # Prefer valid_time as the time axis if 'time' isn't hourly.
        if "valid_time" in out.coords and "time" in out.dims:
            # valid_time may be 1-D here and equal to time; keep 'time'.
            pass
        return out

    # 2-D (time, step) -> stacked 'vt'.
    if "valid_time" not in ds.coords:
        raise RuntimeError(
            "Cannot flatten (time, step) cube without 'valid_time'."
        )

    # De-accumulate accumulated fields (ssrd, sf) along step FIRST, while
    # the forecast-day boundary is unambiguous on the step axis.
    ds = _deaccumulate_along_step(ds)

    # Capture valid_time before stacking creates a multi-index.
    vt_2d = np.asarray(ds["valid_time"].values)  # shape (time, step)

    stacked = ds.stack(vt=("time", "step"))
    vt_vals = vt_2d.reshape(-1)

    # Drop the multi-index cleanly, then attach the true timestamps.
    stacked = stacked.drop_vars(
        ["vt", "time", "step"], errors="ignore"
    )
    stacked = stacked.assign_coords(vt=("vt", vt_vals))
    stacked = stacked.rename({"vt": "time"})

    # Drop scalar/leftover coords.
    drop = [
        c for c in ("surface", "number", "valid_time")
        if c in stacked.coords
    ]
    stacked = stacked.drop_vars(drop, errors="ignore")

    # Sort by true time and remove duplicate timestamps (forecast
    # overlap days), keeping the first occurrence.
    stacked = stacked.sortby("time")
    _, keep = np.unique(
        np.asarray(stacked["time"].values), return_index=True
    )
    stacked = stacked.isel(time=np.sort(keep))

    # After stacking, 'time' lands last; put it first so all variables
    # share a consistent (time, latitude, longitude) order.  This matches
    # the Spencer SZA output and the night-mask expectations downstream.
    spatial = [d for d in stacked.dims if d != "time"]
    stacked = stacked.transpose("time", *spatial)
    return stacked


def _drop_empty_timestamps(ds: xr.Dataset) -> xr.Dataset:
    """Drop timestamps for which NO GRIB message exists at all.

    cfgrib materialises the ``(time, step)`` forecast cube with NaN rows
    for steps that are absent from the file.  After flattening, these
    become phantom timestamps that are spatially **all-NaN**:

    * the previous month's steps 1..23 (23 leading stamps) — only its
      step 24 is shipped;
    * this month's last day step 24 (1 trailing stamp) — it ships in the
      NEXT month's file instead.

    We detect them empirically (``ssrd`` all-NaN across the grid) rather
    than by calendar rule.  The file's genuine first stamp is NOT caught:
    it holds the raw accumulated value (kept by
    :func:`_deaccumulate_along_step`), so it is not all-NaN over land.

    After this drop a monthly file spans exactly
    ``<1st> 00:00 .. <last> 23:00`` — the full calendar month, 24*n_days
    stamps, with no overlap between consecutive files.
    """

    if "time" not in ds.dims or _SSRD not in ds:
        return ds

    ssrd = ds[_SSRD]
    spatial = [d for d in ssrd.dims if d != "time"]
    if not spatial:
        return ds

    has_data = ssrd.notnull().any(dim=spatial)
    vals = (
        has_data.compute().values
        if hasattr(has_data.data, "dask")
        else has_data.values
    )
    keep = np.where(vals)[0]
    n_drop = int(ds.sizes["time"] - keep.size)
    if n_drop == 0:
        return ds

    t = pd.DatetimeIndex(np.asarray(ds["time"].values))
    logger.info(
        "Dropping %d phantom timestamp(s) with no GRIB message "
        "(e.g. %s ...) — these hours live in the neighbouring months' "
        "files.  Remaining span: %s .. %s (%d stamps).",
        n_drop,
        ", ".join(str(x)[:16] for x in t[~vals][:3]),
        t[keep[0]], t[keep[-1]], keep.size,
    )
    return ds.isel(time=keep)


def _ffill_time(ds: xr.Dataset) -> xr.Dataset:
    """Forward-fill NaNs along the ``time`` dim without needing bottleneck.

    xarray's :meth:`Dataset.ffill` imports the optional ``bottleneck``
    package; when it is absent this raises ``ModuleNotFoundError`` at
    compute time.  We first try the native method and fall back to a
    vectorised NumPy forward-fill applied per variable.  Only variables
    that actually contain NaNs are touched, so the cost is minimal.
    """
    try:
        # Fast path if bottleneck is available.
        import bottleneck  # type: ignore  # noqa: F401

        return ds.ffill(dim="time")
    except ImportError:
        pass

    axis_name = "time"
    out = ds
    for var in list(out.data_vars):
        da = out[var]
        if axis_name not in da.dims:
            continue

        def _np_ffill(arr: np.ndarray, ax: int) -> np.ndarray:
            arr = np.moveaxis(arr, ax, 0)
            mask = np.isnan(arr)
            if not mask.any():
                return np.moveaxis(arr, 0, ax)
            n = arr.shape[0]
            # Build an index that holds the last valid position per cell.
            rng = np.arange(n)
            shape = [n] + [1] * (arr.ndim - 1)
            idx = np.where(~mask, rng.reshape(shape), 0)
            np.maximum.accumulate(idx, axis=0, out=idx)
            arr = np.take_along_axis(arr, idx, axis=0)
            return np.moveaxis(arr, 0, ax)

        axis = da.get_axis_num(axis_name)
        filled = xr.apply_ufunc(
            _np_ffill,
            da,
            kwargs={"ax": axis},
            dask="parallelized",
            output_dtypes=[da.dtype],
        )
        out[var] = filled
    return out


def build_monthly_dataset(
    grib_path: Any,
    *,
    night_mask: bool = False,
    time_chunk: int = _TIME_CHUNK,
) -> xr.Dataset:
    """Read one ERA5-Land monthly GRIB and return an analysis-ready ds.

    Parameters
    ----------
    grib_path : path-like
        Monthly all-variables GRIB from the download phase.
    night_mask : bool
        If ``True``, force GHI to 0 where solar zenith >= 90 deg,
        matching COSMO-REA6.  Default ``False`` — off by default for
        ERA5-Land: ssrd is already ~0 at night, so the mask is only a
        small correction, and enabling it flips ocean-NaN GHI cells to
        0 at night (see the ``night_mask`` branch below).
    time_chunk : int
        Dask chunk size along time (hours).  Default 168 (~1 week).

    Returns
    -------
    xarray.Dataset
        Contains the converted raw attributes plus a derived ``GHI``
        variable.  DNI/DHI are intentionally absent (see module docs).
    """
    logger.info("Opening ERA5-Land GRIB: %s", grib_path)

    # ERA5-Land CDS GRIBs contain MULTIPLE hypercubes with DIFFERENT
    # structures.  Empirically a global all-variables month yields:
    #   * a cube with the analysis/forecast variables on a 2-D
    #     (time=forecast-day, step=hour-of-day) axis — e.g. ssrd, t2m,
    #     d2m, u10, v10, sp, sf, fal;
    #   * one or more cubes already on a flat hourly 'time' axis (e.g.
    #     asn); and
    #   * a snow-depth cube (sde) on the (time, step) axis.
    # The 2-D (time, step) cubes must be FLATTENED onto their true
    # hourly 'valid_time' before anything else, and the accumulated
    # fields (ssrd, sf) reset at each forecast day (step boundary).
    # cfgrib's internal xr.merge emits a repeated FutureWarning about the
    # default 'compat' value.  It is harmless (cfgrib handles the merge
    # correctly) and clutters logs, so silence it around the open call.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=FutureWarning,
            message=".*compat.*",
        )
        warnings.filterwarnings(
            "ignore",
            category=FutureWarning,
            message=".*combine.*",
        )
        cubes = cfgrib.open_datasets(
            str(grib_path),
            backend_kwargs={"indexpath": ""},  # no .idx sidecar
        )
    if not cubes:
        raise RuntimeError(f"cfgrib returned no datasets for {grib_path}")

    # Chunk each cube along SPATIAL dims immediately so that lazy ops
    # (shift/stack/arithmetic in de-accumulation and flattening) operate
    # on bounded dask blocks instead of materialising a full
    # (time, step, lat, lon) array — which on a global grid is ~63 GB per
    # variable and triggers eccodes MemoryAllocationError.  A modest
    # lat-chunk keeps peak memory to a few hundred MB per block.
    #
    # NOTE: this bounds memory but cfgrib still decodes one GRIB MESSAGE
    # (a full lat/lon field) at a time.  On a GLOBAL grid each message is
    # ~26 MB; on a EUROPE bbox it is ~1 MB.  If you still hit memory
    # limits on global data, set ERA5_AREA to crop the download.
    def _chunk_spatial(c: xr.Dataset) -> xr.Dataset:
        chunks: dict[Hashable, int] = {}
        for dim in c.dims:
            if dim in ("latitude", "lat", "y"):
                chunks[dim] = min(256, c.sizes[dim])
            elif dim in ("longitude", "lon", "x"):
                chunks[dim] = c.sizes[dim]
            elif dim == "step":
                chunks[dim] = c.sizes[dim]  # keep step whole for diff
            elif dim == "time":
                chunks[dim] = 1  # one forecast day at a time
        return c.chunk(chunks) if chunks else c

    cubes = [_chunk_spatial(c) for c in cubes]

    # Normalise longitude convention BEFORE flatten/merge so all cubes
    # share one axis (an edition-2 cube like 'sde' may arrive on 0..360
    # while the rest are on -180..180; mismatched labels NaN-fill the
    # merge).
    cubes = [_normalize_longitude(c) for c in cubes]

    flattened = [_flatten_to_hourly(c) for c in cubes]
    for c in cubes:
        c.close()

    # Guarantee identical spatial grids before merging.  All cubes are on
    # the same ERA5-Land 0.1-degree grid, but tiny float differences (or a
    # residual convention gap) between cubes would make an outer join
    # explode the lat/lon axes and NaN-fill everything.  Reindex every
    # cube onto the FIRST cube's latitude/longitude with nearest matching.
    ref = flattened[0]
    lat_name = "latitude" if "latitude" in ref.coords else "lat"
    lon_name = "longitude" if "longitude" in ref.coords else "lon"
    ref_lat = ref[lat_name]
    ref_lon = ref[lon_name]
    aligned = []
    for c in flattened:
        if lat_name in c.coords and lon_name in c.coords:
            c = c.reindex(
                {lat_name: ref_lat, lon_name: ref_lon},
                method="nearest",
                tolerance=0.01,  # degrees; snaps float drift, not real gaps
            )
        aligned.append(c)

    # Merge variables side by side.  Spatial dims now match exactly, so
    # only 'time' uses an outer join (union of hourly stamps).
    ds = xr.merge(
        aligned, compat="override", join="outer", combine_attrs="override"
    )

    # Monthly file structure (verified empirically via enumerate_month):
    #   <prev last day>, step 24      -> <1st> 00:00        (1 message)
    #   <1st>..<last-1>, steps 1..24  -> hourly stamps
    #   <last day>,      steps 1..23  -> ... <last> 23:00
    # cfgrib materialises the missing steps (prev month's 1..23 and this
    # month's last step 24) as spatially all-NaN phantom stamps; drop
    # them.  What remains is exactly the calendar month,
    # <1st> 00:00 .. <last> 23:00.  The FIRST stamp keeps its RAW
    # accumulated value (its de-accumulation predecessor lives in the
    # previous month's file); boundary_repair.py converts it to a true
    # hourly increment after all months exist (now automatic, see
    # run_pipeline's STEP 3/3).  This month's own
    # final hour (<last> 23:00 -> 00:00) ships in the NEXT month's file.
    ds = ds.sortby("time")
    ds = _drop_empty_timestamps(ds)

    # Some cubes (e.g. daily 'asn' snow albedo) can leave NaNs at
    # intermediate hourly stamps after the outer-join merge.  Forward-fill
    # along time to carry the last valid value forward.  xarray's native
    # .ffill() relies on the optional 'bottleneck' package; to avoid a
    # hard dependency we try it and fall back to a NumPy implementation.
    if "time" in ds.dims:
        ds = _ffill_time(ds)

    # Chunk along time for the downstream vectorised math.
    if "time" in ds.dims:
        ds = ds.chunk({"time": time_chunk})

    # Compute RH from RAW Kelvin t2m/d2m, BEFORE _convert_units turns
    # them into Celsius (otherwise the Magnus inputs are double-shifted).
    rh = _compute_rh(ds)

    # Unit conversions on the raw fields.
    ds = _convert_units(ds)

    # Derive hourly-mean GHI from accumulated ssrd.
    ghi = _deaccumulate_to_ghi(ds)

    # BOUNDARY BOOKKEEPING.  The file's FIRST timestamp (<1st> 00:00) is
    # the previous month's final hour and arrives without its step-23
    # predecessor.  Instead of NaN, it now carries the RAW accumulated
    # daily total / 3600 — deliberately, so the true increment can be
    # computed later from the previous month's file WITHOUT any extra
    # stored variables:
    #
    #     GHI(<1st> 00:00) = stored_first
    #                        - sum(prev month's GHI over its last day's
    #                              stamps 01:00..23:00)
    #
    # (The daily reset makes raw[step23] equal the sum of the first 23
    # hourly increments, which are exactly the previous file's last-day
    # GHI values.)  boundary_repair.py now runs automatically as
    # run_pipeline's STEP 3/3 — until it does, the first stamp is a
    # physically absurd flux (a whole day's total as one hour) and MUST
    # NOT be used directly.
    ds.attrs["boundary_status"] = (
        "UNREPAIRED: first timestamp holds the raw accumulated daily "
        "total / 3600, not an hourly flux.  Run "
        "weather.providers.era5_land.boundary_repair.repair_boundaries() "
        "before analysis."
    )

    # Scalar wind speed from the u/v components.
    ws = _compute_wind_speed(ds)

    if night_mask:
        # Opt-in only.  Night-masking is OFF by default for ERA5-Land
        # because (a) ssrd is already ~0 at night, so forcing exact 0
        # changes almost nothing, and (b) the mask's original purpose in
        # COSMO — protecting the horizon-divergent DNI = SWDIRS/cos(theta)
        # inversion — does not apply here (ERA5 stores GHI directly; DNI
        # is deferred to dni_pointwise).  Leaving it off also keeps GHI's
        # ocean cells as NaN, consistent with every other variable
        # (masking would flip night-time ocean NaN to 0).
        logger.info("Computing Spencer SZA for night-masking (opt-in)")
        lat2d, lon2d = _lat_lon_arrays(ds)
        zenith = spencer_zenith(
            pd.DatetimeIndex(ds["time"].values), lat2d, lon2d
        )
        masked = mask_night(ghi.values, zenith)
        ghi = ghi.copy(data=masked)

    # Drop raw ssrd (now represented by GHI) and attach derived fields.
    if _SSRD in ds:
        ds = ds.drop_vars(_SSRD)
    ds["GHI"] = ghi
    if rh is not None:
        ds["RH"] = rh
    if ws is not None:
        ds["WS_10M"] = ws

    # ── Cross-provider naming uniformity ────────────────────────────
    # 1) 2 m air temperature: COSMO emits canonical 'T' (degC); match it.
    if "t2m" in ds:
        ds = ds.rename({"t2m": "T"})

    # 1a) Dew point: ERA5-Land's native 'd2m' renamed to canonical
    #     'T_DEW' (2026-07-26), matching COSMO's derived T_DEW
    #     (inverse Magnus-Tetens from T/RELHUM_2M, see
    #     cosmo_rea6/transform.py's compute_dewpoint) and MERRA-2's
    #     renamed T2MDEW. ERA5-Land's is a directly measured/assimilated
    #     dew point, not a derived one -- no formula needed here.
    if "d2m" in ds:
        ds = ds.rename({"d2m": "T_DEW"})

    # 1b) Forecast albedo: MERRA-2 emits 'ALBEDO' natively and COSMO
    #     derives an equivalent (GHI - SOBS_RAD) / GHI quantity under
    #     the same canonical name (see cosmo_rea6/transform.py's
    #     compute_albedo) -- rename 'fal' to match. 'asn' (snow-specific
    #     albedo, a narrower diagnostic with no cross-provider
    #     equivalent) intentionally keeps its cfgrib short name.
    if "fal" in ds:
        ds = ds.rename({"fal": "ALBEDO"})
        ds["ALBEDO"].attrs.setdefault(
            "long_name", "Forecast albedo (total surface reflectivity)"
        )
        ds["ALBEDO"].attrs.setdefault("units", "1")

    # 1c) Surface pressure: COSMO/MERRA-2 both emit 'PS'; rename 'sp' to
    #     match.
    if "sp" in ds:
        ds = ds.rename({"sp": "PS"})

    # 1d) 10 m wind components: canonical 'U_10M'/'V_10M' matches COSMO's
    #     native names and the already-shared 'WS_10M' scalar's naming
    #     style; MERRA-2's 'U10M'/'V10M' get the same treatment (see
    #     merra2/transform.py). ERA5-Land has no 2 m/50 m wind to rename
    #     (MERRA-2-only, no cross-provider equivalent there).
    if "u10" in ds:
        ds = ds.rename({"u10": "U_10M"})
    if "v10" in ds:
        ds = ds.rename({"v10": "V_10M"})

    # 1e) Snowfall: canonical 'SNOWFALL' matches MERRA-2's renamed
    #     'PRECSNOLAND' and COSMO's combined SNOW_CON+SNOW_GSP (see
    #     cosmo_rea6/transform.py's compute_snowfall) -- all three are
    #     the same physical quantity (accumulated mass of solid
    #     precipitation, kg/m^2/h).
    if "sf" in ds:
        ds = ds.rename({"sf": "SNOWFALL"})

    # 1f) Snow depth: canonical 'SNOW_DEPTH' matches COSMO's renamed
    #     H_SNOW/MERRA-2's renamed SNODP -- confirmed 2026-07-30 (real
    #     CDS request payload + raw GRIB metadata + already-processed
    #     2018-03 output all agree) that this dataset's actual
    #     downloaded field is 'snow_depth' (GRIB shortName 'sde',
    #     physical thickness in meters), NOT 'snow_depth_water_
    #     equivalent' (shortName 'sd') as a prior version of
    #     downloaded_attributes.py's entry incorrectly assumed. All
    #     three providers' snow depth are the same physical quantity
    #     after all -- see downloaded_attributes.py's 'snow_depth'
    #     entry for the full verification trail.
    if "sde" in ds:
        ds = ds.rename({"sde": "SNOW_DEPTH"})

    # 2) Spatial dims: COSMO uses (y, x); ERA5-Land arrives on
    #    (latitude, longitude).  We want y/x DIMENSIONS (so downstream
    #    tools written for COSMO work unchanged) while KEEPING the
    #    geographic lat/lon values as readable coordinates.
    #
    #    Renaming a dimension-coordinate directly (latitude -> y) would
    #    MOVE the geographic values into 'y' and lose the 'latitude'
    #    name.  So we first stash lat/lon as independent 1-D coordinate
    #    variables, then rename the dims.
    if "latitude" in ds.dims and "longitude" in ds.dims:
        lat_vals = ds["latitude"].values
        lon_vals = ds["longitude"].values
        ds = ds.rename({"latitude": "y", "longitude": "x"})
        # Re-attach geographic coords on the y/x dims.
        ds = ds.assign_coords(
            latitude=("y", lat_vals),
            longitude=("x", lon_vals),
        )
        # CF attrs on the coordinate variables themselves -- without these,
        # tools that key off standard_name/units instead of variable name
        # (e.g. CDO's grid-type detection in weather.geo.crop) see an
        # unrecognized "generic" grid and refuse to operate, even though
        # xarray's own .sel(method="nearest") never needed them.
        ds["latitude"].attrs = {
            "standard_name": "latitude",
            "long_name": "latitude",
            "units": "degrees_north",
        }
        ds["longitude"].attrs = {
            "standard_name": "longitude",
            "long_name": "longitude",
            "units": "degrees_east",
        }
        # Replace the bare index coords y/x with simple integer indices
        # (optional, keeps them lightweight; comment out to keep floats).
        ds = ds.assign_coords(
            y=("y", np.arange(ds.sizes["y"])),
            x=("x", np.arange(ds.sizes["x"])),
        )

    # We keep the cfgrib short name for the one remaining raw field
    # (asn -- snow-specific albedo, no cross-provider equivalent) so
    # the file is self-describing and matches the eccodes tables.
    # 'fal'/'sp'/'u10'/'v10'/'sf'/'d2m'/'sde' are the exceptions,
    # renamed above to 'ALBEDO'/'PS'/'U_10M'/'V_10M'/'SNOWFALL'/
    # 'T_DEW'/'SNOW_DEPTH' for cross-provider parity.
    ds.attrs.setdefault("provider", "ERA5_LAND")
    ds.attrs.setdefault("Conventions", "CF-1.8")

    # Strip cfgrib provenance attributes.  These 'GRIB_*' keys (and the
    # huge GRIB_missingValue = 3.4e38 sentinel) are inherited from the
    # source GRIB, are irrelevant to the processed data, and break some
    # viewers: Panoply interprets the giant missing-value / packing keys
    # as a display scaling coefficient and raises
    # "Scaling coefficient must be in the range [-1000.0 - 1000.0]".
    # Removing them makes every variable plot cleanly and keeps the file
    # tidy.  NaN already marks missing data (the CF _FillValue), so no
    # information is lost.
    for var in ds.variables:
        drop_keys = [
            k for k in ds[var].attrs
            if k.startswith("GRIB_") or k in ("missing_value",)
        ]
        for k in drop_keys:
            del ds[var].attrs[k]
    for k in [k for k in ds.attrs if k.startswith("GRIB_")]:
        del ds.attrs[k]

    return ds
