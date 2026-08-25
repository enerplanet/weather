"""Cross-provider irradiance derivation (GHI, DHI, DNI).

Implements the DIRINT pressure-aware decomposition algorithm for
providers that only supply total GHI (MERRA-2, ERA5-Land), and a
direct geometrically-consistent formula for COSMO-REA6 which provides
separate beam and diffuse components.

Algorithm selection by provider
--------------------------------
+-------------+-------+-------+-------+----------------------------------+
| Provider    | GHI   | DHI   | DNI   | Method                           |
+=============+=======+=======+=======+==================================+
| COSMO_REA6  | sum   | direct| guard | SWDIFDS+SWDIRS / cos(z) guard    |
| MERRA2      | passthrough | DIRINT| DIRINT | pvlib.dirint (pressure) |
| ERA5_LAND   | /3600 | DIRINT| DIRINT | pvlib.dirint (pressure)          |
+-------------+-------+-------+-------+----------------------------------+

Post-derivation standardisation
---------------------------------
All formula functions apply the following uniformity rules:

1. **Night masking**: zenith >= 90 deg -> 0.0 W/m2 (no floating-point
   artefacts during darkness).
2. **Physical clipping**: all results clipped to [0, inf).
3. **Pressure units**: pvlib.dirint expects raw Pascals; both MERRA-2
   (PS) and ERA5-Land (sp) provide Pa natively — no conversion needed.

Typical usage::

    from weather.common.derived_attributes import apply_derived_fields

    results = apply_derived_fields(
        ds=my_dataset,
        provider="MERRA2",
        sol_pos={"zenith": zenith_array},
        times=pd.date_range(..., tz="UTC"),
    )
    ghi = results["GHI"]   # np.ndarray, night-masked, clipped >= 0
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Solar zenith threshold (degrees) above which irradiance is forced to 0.
NIGHT_ZENITH_DEG: float = 90.0

#: Minimum solar elevation (degrees) for which COSMO-REA6 DNI is
#: reported; below this the direct-beam division is zeroed instead of
#: amplifying near-horizon noise. Single source of truth for this
#: number — ``providers.cosmo_rea6.transform.compute_dni`` imports it
#: rather than hardcoding its own copy (see docs/dni_methodology.md
#: sec 6). Matches ``tests.compare_providers``'s point-wise pvlib
#: closure comparison too (sec 11), for a fair apples-to-apples check.
DNI_ELEVATION_THRESHOLD_DEG: float = 5.0

#: cos(zenith) lower guard for COSMO-REA6 DNI, derived from
#: :data:`DNI_ELEVATION_THRESHOLD_DEG` (elevation 5 deg == zenith 85
#: deg == cos(zenith) ~= 0.087) — below this the direct-beam path is
#: too long and the division diverges unreliably.
_COS_GUARD: float = float(
    np.cos(np.radians(90.0 - DNI_ELEVATION_THRESHOLD_DEG))
)

#: Upper cos(zenith) bound — corrects float32 rounding in a Spencer-
#: formula (or similar) dot-product from pushing cos(zenith) fractionally
#: above 1.0, which would otherwise give DNI < the direct-beam input for
#: an above-threshold cell (physically impossible, since cos(zenith)<=1
#: always). See docs/dni_methodology.md sec 5.2.
_COS_ZENITH_UPPER: float = 1.0

#: Standard atmosphere pressure (Pa) used when the dataset has no
#: surface-pressure variable.
_STD_PRESSURE_PA: float = 101325.0


# ---------------------------------------------------------------------------
# Night masking and physical clipping
# ---------------------------------------------------------------------------

def mask_night(
    arr: np.ndarray,
    zenith: np.ndarray,
    threshold: float = NIGHT_ZENITH_DEG,
) -> np.ndarray:
    """Return *arr* with night-time values set to 0.0.

    Parameters
    ----------
    arr : array-like
        Irradiance values (any shape broadcastable with *zenith*).
    zenith : array-like
        Solar zenith angle in degrees.
    threshold : float
        Zenith angle at/above which the result is set to 0.0.
        Default: :data:`NIGHT_ZENITH_DEG` (90 deg).

    Returns
    -------
    np.ndarray
        Clipped and night-masked array.
    """
    arr = np.asarray(arr, dtype=float)
    z = np.asarray(zenith, dtype=float)
    return np.where(z >= threshold, 0.0, arr).clip(min=0.0)


# ---------------------------------------------------------------------------
# DIRINT pressure-aware decomposition (MERRA-2, ERA5-Land)
# ---------------------------------------------------------------------------

def _dirint_dni(
    ghi: np.ndarray,
    zenith: np.ndarray,
    pressure: np.ndarray | float,
    times: Any,
) -> np.ndarray:
    """Compute DNI via the DIRINT algorithm.

    DIRINT (Perez et al., 1992) is a pressure-aware improvement on
    the DISC model.  It accepts surface pressure in Pascals directly;
    no unit conversion is required.

    Parameters
    ----------
    ghi : array-like
        Global Horizontal Irradiance (W/m2).
    zenith : array-like
        Solar zenith angle (degrees).
    pressure : array-like or float
        Surface pressure in Pascals.  Standard atmosphere (101325 Pa)
        is used automatically if the dataset has no pressure variable.
    times : DatetimeIndex or array-like
        UTC timestamps corresponding to the time axis.

    Returns
    -------
    np.ndarray
        DNI in W/m2, clipped to [0, inf).
    """
    import pvlib  # optional extra (`solar`); see audit_imports

    idx = pd.DatetimeIndex(times)
    ghi_s = pd.Series(np.asarray(ghi, dtype=float), index=idx)
    zen_s = pd.Series(np.asarray(zenith, dtype=float), index=idx)
    pres_s = pd.Series(
        np.broadcast_to(
            np.asarray(pressure, dtype=float),
            ghi_s.shape,
        ).copy(),
        index=idx,
    )

    dni = pvlib.irradiance.dirint(
        ghi=ghi_s,
        solar_zenith=zen_s,
        times=idx,
        pressure=pres_s,
    ).fillna(0.0).clip(lower=0.0)

    return dni.values


def _dirint_dhi(
    ghi: np.ndarray,
    zenith: np.ndarray,
    pressure: np.ndarray | float,
    times: Any,
) -> np.ndarray:
    """Compute DHI = GHI - DNI * cos(zenith) via the DIRINT algorithm.

    Uses the radiation energy-balance equation after computing DNI via
    :func:`_dirint_dni`.  Result is clipped to [0, inf) to prevent
    small negative residuals from floating-point arithmetic.

    Parameters
    ----------
    ghi, zenith, pressure, times
        Same as :func:`_dirint_dni`.

    Returns
    -------
    np.ndarray
        DHI in W/m2, clipped to [0, inf).
    """
    dni = _dirint_dni(ghi, zenith, pressure, times)
    cos_z = np.cos(np.radians(np.asarray(zenith, dtype=float)))
    dhi = np.asarray(ghi, dtype=float) - dni * cos_z
    return dhi.clip(min=0.0)


# ---------------------------------------------------------------------------
# Shared pure formulas
# ---------------------------------------------------------------------------
# Single source of truth for math ALSO used by each provider's own
# transform.py (grid/dask-oriented, performance-tuned). Written with
# duck-typed elementwise operations only (``**``, ``/``, ``.clip()``,
# numpy ufuncs) so a provider's transform.py can call these directly on
# lazy dask-backed xarray.DataArrays without forcing materialisation --
# no ``np.asarray()`` coercion in here. Coercion to plain numpy (for the
# dict-based ``apply_derived_fields`` callers below, and for the unit
# tests in tests/test_derived_attributes.py) happens one layer up, in
# each ``_<provider>_<field>`` wrapper.

def wind_speed(u: Any, v: Any) -> Any:
    """Scalar wind speed WS = sqrt(u**2 + v**2).

    Identical across every provider (COSMO-REA6, ERA5-Land, MERRA-2) --
    the magnitude is frame-invariant, so no vector rotation is needed
    regardless of whether u/v are in a rotated-pole or WGS84 frame.
    """
    return (u ** 2 + v ** 2) ** 0.5


def ghi_from_diffuse_direct(diffuse: Any, direct: Any) -> Any:
    """GHI = diffuse + direct horizontal irradiance, each clipped to
    ``[0, inf)`` *before* summing.

    Clipping each component individually (not just the final sum)
    matters: a single spurious negative component (rare GRIB artefact)
    must not silently cancel out a real positive one.
    """
    return diffuse.clip(min=0.0) + direct.clip(min=0.0)


def dni_from_direct(
    direct: Any,
    cos_zenith_safe: Any,
    elevation_deg: Any,
    elevation_threshold_deg: float = DNI_ELEVATION_THRESHOLD_DEG,
) -> Any:
    """DNI = direct / cos(zenith), zero where solar elevation is below
    *elevation_threshold_deg* (see docs/dni_methodology.md sec 6).

    The caller must pass an already-bounded ``cos_zenith_safe`` --
    clipped to ``[cos(90 - elevation_threshold_deg), 1.0]`` -- and the
    matching ``elevation_deg`` (see sec 5 for why both clip bounds
    matter). A multiplicative mask is used instead of ``where`` so this
    stays correct under both plain numpy (test fixtures) and
    xarray/dask (COSMO's gridded pipeline) without depending on
    ``np.where`` vs ``xr.where`` dispatch.
    """
    dni_raw = direct / cos_zenith_safe
    above_threshold = elevation_deg >= elevation_threshold_deg
    return dni_raw * above_threshold


def magnus_rh(t_celsius: Any, td_celsius: Any) -> Any:
    """Relative humidity (%) via the August-Roche-Magnus approximation
    from temperature and dew point (both degC), clipped to ``[0, 100]``.

    Used by ERA5-Land (the only provider with a dew-point field).
    """
    a, b = 17.625, 243.04
    rh = 100.0 * np.exp(
        (a * td_celsius) / (b + td_celsius) - (a * t_celsius) / (b + t_celsius)
    )
    return rh.clip(0.0, 100.0)


def dewpoint_from_rh(t_celsius: Any, rh_percent: Any) -> Any:
    """Dew point (degC) via the inverse August-Roche-Magnus approximation
    from temperature (degC) and relative humidity (%).

    Algebraic inverse of :func:`magnus_rh` (same a/b constants -- solves
    the same equation for Td instead of RH):
    ``gamma = ln(RH/100) + a*T/(b+T)``; ``Td = b*gamma / (a - gamma)``.
    Accuracy +/-0.35 degC for T in [-40, 50] degC (Alduchov & Eskridge
    1996) -- verified against a standard meteorological reference, not
    just derived algebraically.

    Used by COSMO-REA6, the only provider with neither a native
    dew-point field nor specific humidity already downloaded: it has
    ``T_2M``/``RELHUM_2M`` directly, so this is a free derivation (no
    new attribute needed). Confirmed via DWD's real COSMO-REA6
    ``hourly/2D/`` directory listing that no dew-point field exists
    upstream at all (not just "not downloaded").
    """
    a, b = 17.625, 243.04
    gamma = np.log(rh_percent / 100.0) + (a * t_celsius) / (b + t_celsius)
    return (b * gamma) / (a - gamma)


def bolton_rh(specific_humidity: Any, pressure_pa: Any, t_celsius: Any) -> Any:
    """Relative humidity (%) from specific humidity, pressure, and
    temperature (degC), via the Bolton (1980) saturation-vapor-pressure
    curve, clipped to ``[0, 100]``.

    Used by MERRA-2 (the only provider with specific humidity instead
    of a dew-point field). Deliberately a DIFFERENT formula family from
    :func:`magnus_rh` -- see ``CLAUDE.md``: "RH source differs
    BY-DESIGN ... do NOT unify."
    """
    e = (specific_humidity * pressure_pa) / (0.622 + 0.378 * specific_humidity)
    es = 611.2 * np.exp(17.67 * t_celsius / (t_celsius + 243.5))
    rh = 100.0 * e / es
    return rh.clip(0.0, 100.0)


# ---------------------------------------------------------------------------
# COSMO-REA6 formula functions
# ---------------------------------------------------------------------------

def _cosmo_ghi(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """GHI = SWDIFDS_RAD + SWDIRS_RAD, night-masked."""
    diffuse = np.asarray(ds["SWDIFDS_RAD"], dtype=float)
    direct = np.asarray(ds["SWDIRS_RAD"], dtype=float)
    ghi = ghi_from_diffuse_direct(diffuse, direct)
    return mask_night(ghi, sol_pos["zenith"])


def _cosmo_dhi(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """DHI = SWDIFDS_RAD (diffuse component), night-masked."""
    return mask_night(
        np.asarray(ds["SWDIFDS_RAD"], dtype=float),
        sol_pos["zenith"],
    )


def _cosmo_dni(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """DNI = SWDIRS_RAD / cos(zenith), zeroed below
    :data:`DNI_ELEVATION_THRESHOLD_DEG` elevation.

    ``cos(zenith)`` is clipped to ``[_COS_GUARD, _COS_ZENITH_UPPER]``
    before dividing -- matches
    ``providers.cosmo_rea6.transform.compute_dni``'s ``[1e-3, 1.0]``
    bounds exactly (see docs/dni_methodology.md sec 5): the lower bound
    prevents a division blow-up near the horizon, the upper bound
    corrects float32 rounding that can otherwise push cos(zenith)
    fractionally above 1.0.
    """
    zenith = np.asarray(sol_pos["zenith"], dtype=float)
    cos_zenith_safe = np.clip(np.cos(np.radians(zenith)), _COS_GUARD, _COS_ZENITH_UPPER)
    elevation = 90.0 - zenith
    dni = dni_from_direct(
        np.asarray(ds["SWDIRS_RAD"], dtype=float), cos_zenith_safe, elevation,
    )
    return mask_night(dni, zenith)


def _cosmo_wind_speed(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """Scalar 10 m wind speed ``WS_10M = sqrt(U_10M**2 + V_10M**2)``."""
    u = np.asarray(ds["U_10M"], dtype=float)
    v = np.asarray(ds["V_10M"], dtype=float)
    return wind_speed(u, v)


# ---------------------------------------------------------------------------
# MERRA-2 formula functions
# ---------------------------------------------------------------------------

def _merra2_ghi(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """GHI = SWGDN (instantaneous W/m2), night-masked."""
    return mask_night(
        np.asarray(ds["SWGDN"], dtype=float),
        sol_pos["zenith"],
    )


def _merra2_dhi(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """DHI via DIRINT pressure-aware decomposition.

    Surface pressure is read from ``ds["PS"]`` (Pa).  Falls back to
    standard atmosphere when the key is absent.
    """
    pressure = ds.get("PS", _STD_PRESSURE_PA)
    dhi = _dirint_dhi(
        ds["SWGDN"], sol_pos["zenith"], pressure, times
    )
    return mask_night(dhi, sol_pos["zenith"])


def _merra2_dni(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """DNI via DIRINT pressure-aware decomposition.

    Surface pressure is read from ``ds["PS"]`` (Pa).  Falls back to
    standard atmosphere when the key is absent.
    """
    pressure = ds.get("PS", _STD_PRESSURE_PA)
    dni = _dirint_dni(
        ds["SWGDN"], sol_pos["zenith"], pressure, times
    )
    return mask_night(dni, sol_pos["zenith"])


# ---------------------------------------------------------------------------
# ERA5-Land formula functions
# ---------------------------------------------------------------------------

def _era5_ghi(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """GHI = ssrd / 3600 (J/m2 -> W/m2), night-masked.

    .. note::
       ``ssrd`` must already be **de-accumulated** to a per-hour energy
       increment before calling this (raw multi-step GRIB ``ssrd`` is a
       running total since a forecast day's start, not a per-hour
       value). The gridded pipeline does that de-accumulation in
       ``providers.era5_land.transform._deaccumulate_along_step`` --
       genuinely provider-specific stateful logic over the GRIB
       ``step`` dimension, not a pure elementwise formula, so it isn't
       duplicated here. This function only performs the final
       (shared-in-spirit but ERA5-Land-only) J/m2 -> W/m2 conversion,
       matching ``transform._deaccumulate_to_ghi``'s last two lines.
    """
    ghi = (np.asarray(ds["ssrd"], dtype=float) / 3600.0).clip(min=0.0)
    return mask_night(ghi, sol_pos["zenith"])


def _era5_dhi(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """DHI via DIRINT pressure-aware decomposition.

    Surface pressure is read from ``ds["sp"]`` (Pa).  Falls back to
    standard atmosphere when the key is absent.
    """
    ghi = (np.asarray(ds["ssrd"], dtype=float) / 3600.0).clip(
        min=0.0
    )
    pressure = ds.get("sp", _STD_PRESSURE_PA)
    dhi = _dirint_dhi(ghi, sol_pos["zenith"], pressure, times)
    return mask_night(dhi, sol_pos["zenith"])


def _era5_dni(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """DNI via DIRINT pressure-aware decomposition.

    Surface pressure is read from ``ds["sp"]`` (Pa).  Falls back to
    standard atmosphere when the key is absent.
    """
    ghi = (np.asarray(ds["ssrd"], dtype=float) / 3600.0).clip(
        min=0.0
    )
    pressure = ds.get("sp", _STD_PRESSURE_PA)
    dni = _dirint_dni(ghi, sol_pos["zenith"], pressure, times)
    return mask_night(dni, sol_pos["zenith"])


def _era5_rh(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """Relative humidity (%) via :func:`magnus_rh`.

    Computed from 2 m temperature and 2 m dew point.  Inputs are read
    in **Kelvin** (ERA5-Land raw ``t2m``/``d2m``) and converted to
    Celsius before calling the shared formula.

    .. note::
       The gridded pipeline computes RH inside
       ``providers.era5_land.transform._compute_rh``, from raw Kelvin
       before unit conversion, but calls this same :func:`magnus_rh`
       for the actual math -- pass raw-Kelvin ``t2m``/``d2m`` here too.
    """
    t2m = np.asarray(ds["t2m"], dtype=float)
    d2m = np.asarray(ds["d2m"], dtype=float)
    return magnus_rh(t2m - 273.15, d2m - 273.15)


def _era5_wind_speed(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """Scalar 10 m wind speed ``WS_10M = sqrt(u10**2 + v10**2)`` (m/s)."""
    u = np.asarray(ds["u10"], dtype=float)
    v = np.asarray(ds["v10"], dtype=float)
    return wind_speed(u, v)


def _merra2_rh(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """Relative humidity (%) via :func:`bolton_rh`.

    Inputs: ``QV2M`` (specific humidity, kg/kg), ``PS`` (surface
    pressure, Pa), ``T2M`` (temperature, **already degC** -- MERRA-2's
    ``T2M`` is converted from Kelvin upstream, unlike ERA5-Land's raw
    Kelvin ``t2m``/``d2m``; see
    ``providers.merra2.transform._compute_rh`` for the same formula
    applied to the full grid).
    """
    q = np.asarray(ds["QV2M"], dtype=float)
    p = np.asarray(ds["PS"], dtype=float)
    t = np.asarray(ds["T2M"], dtype=float)
    return bolton_rh(q, p, t)


def _merra2_wind_speed(
    ds: dict[str, Any],
    sol_pos: dict[str, Any],
    times: Any,
) -> np.ndarray:
    """Scalar 10 m wind speed ``WS_10M = sqrt(U10M**2 + V10M**2)`` (m/s)."""
    u = np.asarray(ds["U10M"], dtype=float)
    v = np.asarray(ds["V10M"], dtype=float)
    return wind_speed(u, v)


# ---------------------------------------------------------------------------
# DERIVED_FIELDS registry
# ---------------------------------------------------------------------------

#: Provider-keyed registry mapping field names to metadata and the
#: callable ``formula(ds, sol_pos, times) -> np.ndarray``.
DERIVED_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "COSMO_REA6": {
        "GHI": {
            "description": (
                "Global Horizontal Irradiance "
                "= SWDIFDS_RAD + SWDIRS_RAD"
            ),
            "unit": "W/m2",
            "formula": _cosmo_ghi,
        },
        "DHI": {
            "description": (
                "Diffuse Horizontal Irradiance = SWDIFDS_RAD"
            ),
            "unit": "W/m2",
            "formula": _cosmo_dhi,
        },
        "DNI": {
            "description": (
                "Direct Normal Irradiance "
                "= SWDIRS_RAD / cos(zenith), "
                "guarded at zenith > 85 deg"
            ),
            "unit": "W/m2",
            "formula": _cosmo_dni,
        },
        "WS_10M": {
            "description": "Scalar 10 m wind speed from U_10M/V_10M components",
            "unit": "m/s",
            "formula": _cosmo_wind_speed,
        },
    },
    "MERRA2": {
        "GHI": {
            "description": (
                "Global Horizontal Irradiance = SWGDN"
            ),
            "unit": "W/m2",
            "formula": _merra2_ghi,
        },
        "DHI": {
            "description": (
                "Diffuse Horizontal Irradiance "
                "via DIRINT pressure-aware decomposition"
            ),
            "unit": "W/m2",
            "formula": _merra2_dhi,
        },
        "DNI": {
            "description": (
                "Direct Normal Irradiance "
                "via DIRINT pressure-aware decomposition"
            ),
            "unit": "W/m2",
            "formula": _merra2_dni,
        },
        "RH": {
            "description": (
                "Relative Humidity: QV2M converted to vapor pressure, "
                "divided by Bolton (1980) saturation vapor pressure at T2M"
            ),
            "unit": "%",
            "formula": _merra2_rh,
        },
        "WS_10M": {
            "description": "Scalar 10 m wind speed from U10M/V10M components",
            "unit": "m/s",
            "formula": _merra2_wind_speed,
        },
    },
    # ERA5-Land supplies only total shortwave (ssrd), so only GHI is a
    # *gridded* derived field.  DNI/DHI require a per-site decomposition
    # (DIRINT/DISC) whose pvlib implementation is 1-D-in-time and cannot
    # broadcast over (time, y, x); attempting it on the grid raises.
    # Those are therefore provided as an opt-in point/region helper in
    # ``weather.providers.era5_land.dni_pointwise`` rather than here.
    "ERA5_LAND": {
        "GHI": {
            "description": (
                "Global Horizontal Irradiance: ssrd de-accumulated "
                "per UTC day, divided by 3600 s (hourly-mean flux)"
            ),
            "unit": "W/m2",
            "formula": _era5_ghi,
        },
        "RH": {
            "description": (
                "Relative Humidity via Magnus formula from 2 m "
                "temperature and 2 m dew point"
            ),
            "unit": "%",
            "formula": _era5_rh,
        },
        "WS_10M": {
            "description": (
                "Scalar 10 m wind speed from u/v components"
            ),
            "unit": "m/s",
            "formula": _era5_wind_speed,
        },
    },
}

#: Canonical output variable names (provider-agnostic).
OUTPUT_NAMES: tuple[str, ...] = ("GHI", "DHI", "DNI")

#: Valid provider keys.
PROVIDERS: tuple[str, ...] = tuple(DERIVED_FIELDS.keys())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_derived_fields(
    ds: dict[str, Any],
    provider: str,
    sol_pos: dict[str, Any],
    times: Any,
    fields: list[str] | None = None,
) -> dict[str, np.ndarray]:
    """Compute derived irradiance fields for a given provider.

    Parameters
    ----------
    ds : dict-like or xarray.Dataset
        Source dataset keyed by provider-specific raw variable names.
        Variables are coerced to ``np.ndarray`` internally so both
        plain dicts and xarray Datasets are accepted.
    provider : str
        One of ``"COSMO_REA6"``, ``"MERRA2"``, ``"ERA5_LAND"``.
    sol_pos : dict
        Solar position dict with at minimum a ``"zenith"`` key
        (degrees, 1-D, same length as the time axis).
    times : DatetimeIndex or array-like
        UTC timestamps.  Must be timezone-aware for DIRINT.
    fields : list[str], optional
        Subset of fields to compute.  Default: all fields registered
        for the provider (``["GHI", "DHI", "DNI"]``).

    Returns
    -------
    dict[str, np.ndarray]
        Mapping field name -> numpy array, night-masked and clipped
        to ``[0, inf)``.

    Raises
    ------
    ValueError
        If *provider* is not in :data:`PROVIDERS` or a requested
        *field* is not registered for that provider.

    Examples
    --------
    >>> results = apply_derived_fields(
    ...     ds=my_ds,
    ...     provider="MERRA2",
    ...     sol_pos={"zenith": zenith_arr},
    ...     times=pd.date_range("2018-01-01", periods=N,
    ...                         freq="h", tz="UTC"),
    ... )
    >>> ghi = results["GHI"]
    """
    registry = DERIVED_FIELDS.get(provider)
    if registry is None:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Valid options: {list(PROVIDERS)}"
        )

    requested = fields or list(registry.keys())
    unknown = [f for f in requested if f not in registry]
    if unknown:
        raise ValueError(
            f"Fields {unknown!r} not registered for "
            f"provider {provider!r}."
        )

    results: dict[str, np.ndarray] = {}
    for name in requested:
        logger.debug(
            "Computing %s for provider %s", name, provider
        )
        results[name] = registry[name]["formula"](
            ds, sol_pos, times
        )
    return results


def field_metadata(provider: str, field: str) -> dict[str, Any]:
    """Return description and unit for a derived field.

    Parameters
    ----------
    provider : str
        Provider key (see :data:`PROVIDERS`).
    field : str
        Field name (e.g. ``"GHI"``).

    Returns
    -------
    dict
        ``{"description": str, "unit": str}``.
    """
    registry = DERIVED_FIELDS.get(provider, {})
    entry = registry.get(field, {})
    return {
        "description": entry.get("description", ""),
        "unit": entry.get("unit", ""),
    }
