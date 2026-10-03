"""MERRA-2 daily NetCDFs -> analysis-ready monthly dataset.

Reads the daily per-collection NetCDF4 files produced by
:mod:`~weather.providers.merra2.download` (already cropped server-side to
the configured Europe box via OPeNDAP — see
:mod:`~weather.providers.merra2.downloader`), merges the ``rad``/``slv``
collections, applies unit conversions (see
:mod:`~weather.providers.merra2.downloaded_attributes`), derives GHI,
wind speed, and relative humidity, and returns a single
:class:`xarray.Dataset` ready for export.

Design goals
------------
* **Consistency with ERA5-Land/COSMO-REA6.**  Output uses the same
  ``(time, y, x)`` dim convention (geographic ``latitude``/``longitude``
  kept as coordinates), CF-1.8 attrs, and monthly output granularity as
  ERA5-Land, so downstream code (``common.merge``, percentile analysis)
  works unchanged.
* **GHI is simpler than ERA5-Land's.**  MERRA-2's ``SWGDN`` is an
  *instantaneous* flux, not an accumulated one — no de-accumulation, no
  cross-month boundary bookkeeping is needed (unlike ERA5-Land's
  ``ssrd``).  GHI is derived via
  :func:`weather.common.derived_attributes.apply_derived_fields` with
  ``fields=["GHI"]`` only — DHI/DNI are intentionally NOT computed here
  (see module note below).
* **DNI/DHI are NOT precomputed.**  The registry's MERRA-2 DHI/DNI
  formulas use pvlib's DIRINT, which is 1-D-per-site and cannot
  broadcast over a full ``(time, y, x)`` grid — the same limitation that
  made ERA5-Land defer DNI/DHI to an opt-in point-wise helper
  (:mod:`weather.providers.era5_land.dni_pointwise`).  A MERRA-2
  equivalent (``merra2/dni_pointwise.py``) is a documented future
  extension, not built in this phase.
* **RH is specific-humidity-based.**  Unlike COSMO (direct RELHUM_2M) or
  ERA5-Land (dew-point Magnus formula), MERRA-2 has no direct RH or
  dew-point field — only ``QV2M`` (specific humidity).  See
  :func:`_compute_rh` and ``docs/MERRA2_PIPELINE_GUIDE.md`` for the
  derivation.
* **Grid is native, not regridded.**  MERRA-2's native grid (0.5 deg lat
  x 0.625 deg lon) is coarser than and not cell-aligned with ERA5-Land's
  (0.1 deg).  This module crops to the *same geographic* Europe box but
  leaves data on MERRA-2's own grid — no interpolation.  Cross-provider
  regridding, if ever needed, is a separate future task.
* **Timestamps are on the half-hour, by design.**  ``tavg1_2d`` values
  are hourly time-averages labeled at the midpoint, so ``time`` lands on
  ``HH:30`` (unlike ERA5-Land/COSMO's ``HH:00``).  This is intentionally
  NOT relabeled to ``HH:00`` here — see ``docs/MERRA2_PIPELINE_GUIDE.md``
  ("Timestamp convention"). Any cross-provider merge must handle the
  30-min offset explicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import xarray as xr

from ...common.derived_attributes import apply_derived_fields, bolton_rh, wind_speed
from ...common.solar_position import spencer_zenith

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unit conversions (mirrors downloaded_attributes.ATTRIBUTES conversions)
# ---------------------------------------------------------------------------

def _convert_units(ds: xr.Dataset) -> xr.Dataset:
    """Apply the unit conversions declared in ``downloaded_attributes``.

    ``T2M``/``T2MDEW`` (Kelvin -> Celsius) and ``PRECSNOLAND``
    (kg/m^2/s -> kg/m^2/h, matching COSMO/ERA5-Land's snowfall
    convention); every other raw attribute is already in its target
    unit.
    """
    out = ds
    if "T2M" in out:
        out["T2M"] = out["T2M"] - 273.15
        out["T2M"].attrs["units"] = "degC"
    if "T2MDEW" in out:
        out["T2MDEW"] = out["T2MDEW"] - 273.15
        out["T2MDEW"].attrs["units"] = "degC"
    if "PRECSNOLAND" in out:
        out["PRECSNOLAND"] = out["PRECSNOLAND"] * 3600.0
        out["PRECSNOLAND"].attrs["units"] = "kg/m^2/h"
    return out


# ---------------------------------------------------------------------------
# Relative humidity from specific humidity (Bolton 1980)
# ---------------------------------------------------------------------------

def _compute_rh(ds: xr.Dataset) -> xr.DataArray | None:
    """Relative humidity (%) from ``QV2M`` (specific humidity), ``T2M``
    (already Celsius), and ``PS`` (surface pressure, Pa).

    Uses the standard specific-humidity -> vapor-pressure conversion
    combined with the Bolton (1980) saturation-vapor-pressure curve::

        e  = QV2M * PS / (0.622 + 0.378 * QV2M)        # vapor pressure, Pa
        es = 611.2 * exp(17.67*T / (T + 243.5))        # Bolton 1980, Pa
        RH = 100 * e / es, clipped to [0, 100]

    IMPORTANT: unlike ERA5-Land's Magnus RH (which needs *pre-conversion*
    Kelvin inputs), this Bolton formula needs ``T2M`` in **Celsius**, so
    call this **after** :func:`_convert_units`, not before — the reverse
    ordering of ERA5-Land's RH.  See ``docs/MERRA2_PIPELINE_GUIDE.md``
    for the full derivation.

    Returns
    -------
    xarray.DataArray or None
        RH in percent, clipped to [0, 100]; ``None`` if any input
        variable is absent.
    """
    missing = [v for v in ("QV2M", "T2M", "PS") if v not in ds]
    if missing:
        logger.warning(
            "Missing %s; skipping RH (have %s)", missing, list(ds.data_vars),
        )
        return None

    q = ds["QV2M"]
    t_degc = ds["T2M"]
    p = ds["PS"]

    # Formula is derived_attributes.bolton_rh -- single source of
    # truth, also used by derived_attributes._merra2_rh.
    rh = cast(xr.DataArray, bolton_rh(q, p, t_degc))
    rh.name = "RH"
    rh.attrs = {
        "long_name": "Relative Humidity",
        "units": "%",
        "description": (
            "Specific humidity (QV2M) converted to vapor pressure, "
            "divided by Bolton (1980) saturation vapor pressure at T2M"
        ),
        "derivation": "RH = 100 * e(QV2M,PS) / es_bolton(T2M)",
    }
    return rh


def _compute_wind_speed(ds: xr.Dataset) -> xr.DataArray | None:
    """Scalar 10 m wind speed ``WS_10M = sqrt(U10M**2 + V10M**2)``."""
    if "U10M" not in ds or "V10M" not in ds:
        logger.warning(
            "U10M and/or V10M missing; skipping WS_10M (have %s)",
            list(ds.data_vars),
        )
        return None

    # Formula is derived_attributes.wind_speed -- single source of
    # truth, shared with every provider.
    ws = cast(xr.DataArray, wind_speed(ds["U10M"], ds["V10M"]))
    ws.name = "WS_10M"
    ws.attrs = {
        "long_name": "Wind speed at 10 m",
        "units": "m/s",
        "description": "scalar wind speed sqrt(U10M^2 + V10M^2)",
        "derivation": "WS_10M = sqrt(U10M**2 + V10M**2)",
    }
    return ws


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_monthly_dataset(
    rad_paths: list[Path],
    slv_paths: list[Path],
    lnd_paths: list[Path],
    *,
    year: int,
    month: int,
) -> xr.Dataset:
    """Build one month's analysis-ready dataset from daily MERRA-2 files.

    Parameters
    ----------
    rad_paths : list[Path]
        Daily ``rad`` collection files (``SWGDN``, ``ALBEDO``) for this
        month, one per day.
    slv_paths : list[Path]
        Daily ``slv`` collection files (``T2M``, ``T2MDEW``, ``U/V``
        winds at 2/10/50 m, ``PS``, ``QV2M``) for this month, one per
        day.
    lnd_paths : list[Path]
        Daily ``lnd`` collection files (``SNODP``, ``PRECSNOLAND``) for
        this month, one per day.
    year, month : int
        Calendar year/month these files cover.

    Returns
    -------
    xarray.Dataset
        Contains the converted raw attributes plus derived ``GHI``,
        ``WS_10M``, and ``RH``.  DNI/DHI are intentionally absent (see
        module docs).
    """
    logger.info(
        "Building MERRA-2 monthly dataset %d-%02d from %d rad + %d slv + "
        "%d lnd daily file(s)",
        year, month, len(rad_paths), len(slv_paths), len(lnd_paths),
    )

    ds_rad = xr.open_mfdataset(
        sorted(str(p) for p in rad_paths), combine="by_coords",
    )
    ds_slv = xr.open_mfdataset(
        sorted(str(p) for p in slv_paths), combine="by_coords",
    )
    ds_lnd = xr.open_mfdataset(
        sorted(str(p) for p in lnd_paths), combine="by_coords",
    )

    # Same bbox/day requests against the same native grid -> spatial and
    # time coords already match exactly; no reindex-tolerance dance
    # needed here, unlike ERA5-Land's multi-cube GRIB alignment problem.
    ds = xr.merge(
        [ds_rad, ds_slv, ds_lnd], join="exact", combine_attrs="override",
    )
    ds_rad.close()
    ds_slv.close()
    ds_lnd.close()

    ds = ds.sortby("time")

    # Unit conversions (T2M K->degC).
    ds = _convert_units(ds)

    # Derive hourly GHI = SWGDN directly (instantaneous, night-masked).
    # Only "GHI" is requested from the registry — NOT "DHI"/"DNI" — to
    # sidestep pvlib DIRINT's 1-D-per-site broadcasting limitation (see
    # module docstring).
    lat_name = "lat" if "lat" in ds.coords else "latitude"
    lon_name = "lon" if "lon" in ds.coords else "longitude"
    lat2d, lon2d = np.meshgrid(
        ds[lat_name].values, ds[lon_name].values, indexing="ij",
    )
    times = pd.DatetimeIndex(ds["time"].values)
    zenith = spencer_zenith(times, lat2d, lon2d)
    sol_pos: dict[str, Any] = {"zenith": zenith}
    results = apply_derived_fields(
        cast("dict[str, Any]", ds), "MERRA2", sol_pos, times, fields=["GHI"],
    )
    ghi = xr.DataArray(
        results["GHI"],
        dims=ds["SWGDN"].dims,
        coords=ds["SWGDN"].coords,
        attrs={
            "long_name": "Global Horizontal Irradiance",
            "units": "W/m^2",
            "description": (
                "SWGDN, night-masked (instantaneous, no de-accumulation "
                "needed unlike ERA5-Land's ssrd)"
            ),
            "derivation": "MERRA2 GHI = mask_night(SWGDN, zenith)",
        },
    )

    ws = _compute_wind_speed(ds)
    rh = _compute_rh(ds)  # after T2M K->degC conversion above (see docstring)

    if "SWGDN" in ds:
        ds = ds.drop_vars("SWGDN")
    ds["GHI"] = ghi
    if ws is not None:
        ds["WS_10M"] = ws
    if rh is not None:
        ds["RH"] = rh

    # ── Cross-provider naming uniformity ────────────────────────────
    # 2 m air temperature: COSMO/ERA5-Land emit canonical 'T' (degC);
    # match it (T2M was already K->degC converted above by
    # _convert_units -- _compute_rh's Bolton formula depends on it still
    # being named "T2M" at that point, so this rename must happen after
    # _compute_rh, not inside _convert_units).
    if "T2M" in ds:
        ds = ds.rename({"T2M": "T"})

    # Dew point: MERRA-2's native 'T2MDEW' (free within the
    # already-fetched slv collection) renamed to canonical 'T_DEW',
    # matching COSMO's derived T_DEW (inverse Magnus-Tetens) and
    # ERA5-Land's renamed d2m. Directly measured/assimilated here too,
    # like ERA5-Land's -- no formula needed.
    if "T2MDEW" in ds:
        ds = ds.rename({"T2MDEW": "T_DEW"})

    # 10/2/50 m wind components: canonical 'U_10M'/'V_10M' matches
    # COSMO's native names and ERA5-Land's renamed 'u10'/'v10' (see
    # era5_land/transform.py) and the already-shared 'WS_10M' scalar's
    # naming style. U2M/V2M/U50M/V50M have no cross-provider equivalent
    # (COSMO/ERA5-Land don't fetch 2 m/50 m wind) but get the same
    # underscore style applied for internal consistency.
    _wind_renames = {
        "U10M": "U_10M", "V10M": "V_10M",
        "U2M": "U_2M", "V2M": "V_2M",
        "U50M": "U_50M", "V50M": "V_50M",
    }
    ds = ds.rename({k: v for k, v in _wind_renames.items() if k in ds})

    # Snow depth: COSMO's H_SNOW and MERRA-2's SNODP are both physical
    # snow depth in meters (directly comparable) -- canonical
    # 'SNOW_DEPTH' unifies them. ERA5-Land's 'sd' is a DIFFERENT
    # physical quantity (water-equivalent depth) and is intentionally
    # NOT given this name (see era5_land/transform.py).
    if "SNODP" in ds:
        ds = ds.rename({"SNODP": "SNOW_DEPTH"})

    # Snowfall: canonical 'SNOWFALL' matches ERA5-Land's renamed 'sf'
    # and COSMO's combined SNOW_CON+SNOW_GSP (see cosmo_rea6/transform
    # .py's compute_snowfall) -- all three are the same physical
    # quantity (accumulated mass of solid precipitation, kg/m^2/h).
    if "PRECSNOLAND" in ds:
        ds = ds.rename({"PRECSNOLAND": "SNOWFALL"})

    # Spatial dims: COSMO/ERA5-Land use (y, x); MERRA-2 arrives on
    # (lat, lon).  Stash geographic lat/lon as coordinates, then rename
    # the dims, matching era5_land/transform.py's approach exactly.
    lat_name = "lat" if "lat" in ds.dims else "latitude"
    lon_name = "lon" if "lon" in ds.dims else "longitude"
    if lat_name in ds.dims and lon_name in ds.dims:
        lat_vals = ds[lat_name].values
        lon_vals = ds[lon_name].values
        ds = ds.rename({lat_name: "y", lon_name: "x"})
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
        ds = ds.assign_coords(
            y=("y", np.arange(ds.sizes["y"])),
            x=("x", np.arange(ds.sizes["x"])),
        )

    # Drop raw GES DISC per-day global attrs (Filename, GranuleID,
    # RangeBeginningDate, history, ...) inherited from whichever daily
    # file `xr.merge(..., combine_attrs="override")` picked first above
    # — otherwise they'd misleadingly describe day 1 of the source data
    # rather than the merged month, unlike COSMO/ERA5-Land's clean
    # global-attr set (era5_land/transform.py strips GRIB_* the same way).
    ds.attrs.clear()
    ds.attrs["provider"] = "MERRA2"
    ds.attrs["Conventions"] = "CF-1.8"
    ds.attrs["grid_note"] = (
        "Europe box (same N/W/S/E as ERA5-Land), MERRA-2's own native "
        "0.5x0.625 deg grid — NOT regridded/interpolated onto ERA5-Land's "
        "0.1 deg grid."
    )

    return ds
