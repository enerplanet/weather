"""Transform raw COSMO-REA6 GRIB fields into analysis-ready variables.

Reads decompressed GRIB files with :mod:`xarray` + ``cfgrib``, applies unit
conversions, computes derived fields (GHI, DHI, WS_10M, ALBEDO, SNOWFALL,
T_DEW, DNI), and assembles :class:`xarray.Dataset` objects with the
COSMO-REA6 rotated-pole coordinates and auxiliary WGS84 ``latitude`` /
``longitude`` coordinates.

Key conversions and derived variables:

+----------+----------+-------------+----------------------------------+
| Field    | Raw unit | Output unit | Formula / source                 |
+==========+==========+=============+==================================+
| T        | K        | °C          | T_2M − 273.15                    |
| GHI      | W/m²     | W/m²        | SWDIFDS_RAD + SWDIRS_RAD         |
| DHI      | W/m²     | W/m²        | SWDIFDS_RAD                      |
| WS_10M   | m/s      | m/s         | √(U_10M² + V_10M²)               |
| ALBEDO   | 1        | 1           | (GHI − SOBS_RAD) / GHI           |
| SNOWFALL | kg/m²    | kg/m²/h     | SNOW_CON + SNOW_GSP              |
| T_DEW    | -        | °C          | inverse Magnus(T, RELHUM_2M)     |
| DNI      | W/m²     | W/m²        | SWDIRS_RAD / cos(θ_z) [exp.]     |
+----------+----------+-------------+----------------------------------+

See :func:`compute_dni` for the DNI methodology and
``docs/dni_methodology.md`` for a full algorithm comparison
(Spencer 1971 vs. Meeus vs. NREL SPA).

Typical usage::

    from weather.providers.cosmo_rea6.transform import (
        open_grib_month, build_annual_dataset, compute_dni,
    )
    ds_jan = open_grib_month("SWDIRS_RAD", 2018, 1)
    ds_year = build_annual_dataset(2018)
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ...common.cf_conventions import attach_cf_latlon_attrs
from ...common.derived_attributes import (
    DNI_ELEVATION_THRESHOLD_DEG,
    dewpoint_from_rh,
    dni_from_direct,
    ghi_from_diffuse_direct,
    wind_speed,
)
from .downloaded_attributes import ATTRIBUTES, canonical_name, passthrough_attrs

if TYPE_CHECKING:
    import xarray  # type: ignore[import-untyped]

    from ...geo.bbox import BBox

logger = logging.getLogger(__name__)


def _import_xarray():
    """Lazy-import xarray (heavy dependency, not needed at module level)."""
    try:
        import xarray as xr  # type: ignore[import-untyped]
        return xr
    except ImportError as exc:
        raise ImportError(
            "xarray is required for weather transformation.  "
            "Install with: conda install conda-forge::xarray"
        ) from exc


# ---------------------------------------------------------------------------
# GRIB reading
# ---------------------------------------------------------------------------

def _open_grib_robust(
    grb_path: Path,
    attribute: str,
    xr: Any,
) -> Any:
    """Open a GRIB file, handling ``uvRelativeToGrid`` ambiguity.

    Strategy
    --------
    1. Try without any ``filter_by_keys`` — works for the vast majority
       of COSMO-REA6 files.
    2. If cfgrib raises ``DatasetBuildError`` (file contains both
       ``uvRelativeToGrid=0`` and ``uvRelativeToGrid=1`` messages), retry
       with each value in turn and return the first non-empty dataset.

    This avoids hard-coding a per-attribute flag, which breaks for months
    where DWD packed the same attribute with a different ``uvRelativeToGrid``
    encoding than usual.
    """
    _common = {"engine": "cfgrib", "chunks": {"time": 168}}

    # Pass 1 — no filter (handles ~22/24 years without any issue)
    try:
        ds = xr.open_dataset(str(grb_path), **_common)
        if ds.data_vars:
            return ds
    except Exception as exc:
        # Catch cfgrib's DatasetBuildError for uvRelativeToGrid ambiguity
        error_msg = str(exc)
        if "multiple values for unique key" not in error_msg:
            # Re-raise non-ambiguity errors (file not found, corrupt GRIB)
            raise RuntimeError(
                f"Failed to open GRIB file {grb_path}: {error_msg}"
            ) from exc

    # Pass 2 — ambiguous file; try uvRelativeToGrid=0 then =1
    for uv_flag in (0, 1):
        try:
            ds = xr.open_dataset(
                str(grb_path),
                filter_by_keys={"uvRelativeToGrid": uv_flag},
                **_common,
            )
            if ds.data_vars:
                logger.debug(
                    "%s: resolved with uvRelativeToGrid=%d",
                    grb_path.name, uv_flag,
                )
                return ds
        except OSError as exc:
            # File I/O errors - log and continue to next flag
            logger.debug(
                "uvRelativeToGrid=%d failed for %s: %s",
                uv_flag, grb_path.name, exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            # Other errors (e.g., cfgrib parsing errors) - log and continue
            logger.debug(
                "uvRelativeToGrid=%d raised unexpected error for %s: %s",
                uv_flag, grb_path.name, exc,
            )
            continue

    raise RuntimeError(
        f"Could not open GRIB file with any uvRelativeToGrid filter: "
        f"{grb_path}"
    )


def open_grib_month(
    attribute: str,
    year: int,
    month: int,
    *,
    grb_dir: Path | None = None,
) -> xarray.Dataset:
    """Open a single monthly GRIB file as an xarray Dataset.

    Parameters
    ----------
    attribute : str
        COSMO-REA6 attribute name (e.g. ``"T_2M"``).
    year, month : int
        Target period.
    grb_dir : Path, optional
        Directory containing decompressed ``.grb`` files
        (per-attribute sub-dirs).
        Defaults to ``<work_dir>/decompress/<attribute>/``.

    Returns
    -------
    xarray.Dataset
        Dataset with dimensions ``(time, y, x)`` in native rotated-pole
        coordinates.
    """
    xr = _import_xarray()
    from .config import get_config

    cfg = get_config()
    if grb_dir is None:
        grb_dir = cfg["decompress_dir"] / attribute

    grb_name = f"{attribute}.2D.{year}{month:02d}.grb"
    grb_path = grb_dir / grb_name

    if not grb_path.exists():
        raise FileNotFoundError(f"GRIB file not found: {grb_path}")

    logger.info("Opening GRIB: %s", grb_path)
    return _open_grib_robust(grb_path, attribute, xr)


def open_grib_year(
    attribute: str,
    year: int,
    *,
    grb_dir: Path | None = None,
) -> xarray.Dataset:
    """Open and concatenate all monthly GRIB files for one attribute/year.

    All twelve months (January–December) are loaded automatically.
    Uses :func:`xarray.open_mfdataset` with ``parallel=True`` for
    concurrent I/O across months via dask.delayed.

    Parameters
    ----------
    attribute : str
        COSMO-REA6 attribute name.
    year : int
        Target year.
    grb_dir : Path, optional
        Root directory for decompressed GRIBs.

    Returns
    -------
    xarray.Dataset
        Concatenated along the ``time`` dimension.
    """
    xr = _import_xarray()
    from .config import get_config

    cfg = get_config()
    if grb_dir is None:
        grb_dir = cfg["decompress_dir"]

    attr_dir = grb_dir / attribute
    paths = []
    for m in range(1, 13):
        p = attr_dir / f"{attribute}.2D.{year}{m:02d}.grb"
        if not p.exists():
            raise FileNotFoundError(f"GRIB file not found: {p}")
        paths.append(str(p))

    logger.info(
        "Opening %d GRIB files for %s (parallel=True)",
        len(paths), attribute,
    )
    combined = xr.open_mfdataset(
        paths,
        engine="cfgrib",
        combine="nested",
        concat_dim="time",
        parallel=True,
        chunks={"time": 168},  # ~1 week per chunk; fits in memory
        compat="override",
        coords="minimal",
        data_vars="minimal",
    )
    logger.info(
        "Loaded %s for %d: %d timesteps, grid %s",
        attribute, year, combined.sizes["time"],
        "x".join(str(s) for s in combined.sizes.values()),
    )
    return combined


# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------

def convert_temperature(
    ds: xarray.Dataset, var_name: str = "t2m"
) -> xarray.DataArray:
    """Convert temperature from Kelvin to Celsius.

    Parameters
    ----------
    ds : xarray.Dataset
        Dataset containing the temperature variable.
    var_name : str
        Name of the temperature variable in the dataset
        (cfgrib default: ``"t2m"``).

    Returns
    -------
    xarray.DataArray
        Temperature in °C.
    """
    da = ds[var_name]
    # Guard against double-conversion: cfgrib may pre-decode to °C.
    # Sample one cell rather than calling .max() to avoid materialising
    # the full dask array.
    if hasattr(da.values, 'flat'):
        sample = float(da.values.flat[0])
    else:
        sample = float(da.isel({d: 0 for d in da.dims}).values)
    if sample > 100:  # clearly Kelvin (typical range 230–320 K)
        logger.info(
            "Converting T_2M from Kelvin to Celsius (sample=%.1f K)",
            sample,
        )
        da = da - 273.15
        da.attrs["units"] = "degC"
    else:
        logger.info(
            "T_2M appears to already be in Celsius (sample=%.1f)",
            sample,
        )
        da.attrs["units"] = "degC"
    da.attrs["long_name"] = "Temperature at 2m"
    return da


def compute_dewpoint(
    t_celsius: xarray.DataArray,
    ds_rh: xarray.Dataset,
    rh_var: str = "RELHUM_2M",
) -> xarray.DataArray:
    """Derive 2 m dew point via the inverse Magnus-Tetens formula.

    COSMO-REA6 has no native dew-point field at all (confirmed via
    DWD's real ``hourly/2D/`` directory listing -- not just "not
    downloaded"), but it does have both ``T_2M`` and ``RELHUM_2M``
    already, so this is a free derivation: no new attribute, no new
    download. See :func:`~weather.common.derived_attributes.
    dewpoint_from_rh` for the formula and its verification.

    Parameters
    ----------
    t_celsius : xarray.DataArray
        Temperature, ALREADY Celsius-converted (i.e. the output of
        :func:`convert_temperature`, not the raw Kelvin ``T_2M``
        dataset) -- the Magnus-Tetens formula expects Celsius.
    ds_rh : xarray.Dataset
        Dataset with the raw ``RELHUM_2M`` field (%).

    Returns
    -------
    xarray.DataArray
        Dew point in degC.
    """
    rh = _resolve_var(ds_rh, rh_var)
    td = dewpoint_from_rh(t_celsius, rh)
    td.attrs = {
        "units": "degC",
        "long_name": "Dew point temperature at 2m",
        "description": (
            "Derived from T_2M and RELHUM_2M via the inverse "
            "August-Roche-Magnus formula (COSMO has no native "
            "dew-point field)."
        ),
        "derivation": "T_DEW = dewpoint_from_rh(T, RELHUM_2M)",
    }
    return td


def compute_ghi(
    ds_diffuse: xarray.Dataset,
    ds_direct: xarray.Dataset,
    diffuse_var: str = "SWDIFDS_RAD",
    direct_var: str = "SWDIRS_RAD",
) -> xarray.DataArray:
    """Compute Global Horizontal Irradiance: GHI = diffuse + direct.

    Parameters
    ----------
    ds_diffuse : xarray.Dataset
        Dataset with the SWDIFDS_RAD field.
    ds_direct : xarray.Dataset
        Dataset with the SWDIRS_RAD field.
    diffuse_var, direct_var : str
        Variable names inside the datasets.  cfgrib sometimes lowercases
        or uses short_name; the function tries common alternatives.

    Returns
    -------
    xarray.DataArray
        GHI in W/m².

    Notes
    -----
    Both inputs are clipped to ``[0, ∞)`` before summing — negative
    irradiance (rare GRIB artefact) is physically impossible. The
    actual formula is :func:`~weather.common.derived_attributes.
    ghi_from_diffuse_direct` (single source of truth, also used by
    ``derived_attributes._cosmo_ghi``).
    """
    diffuse = _resolve_var(ds_diffuse, diffuse_var)
    direct = _resolve_var(ds_direct, direct_var)

    ghi = ghi_from_diffuse_direct(diffuse, direct)
    ghi.attrs = {"units": "W/m2", "long_name": "Global Horizontal Irradiance"}
    return ghi


def compute_dhi(
    ds_diffuse: xarray.Dataset,
    diffuse_var: str = "SWDIFDS_RAD",
) -> xarray.DataArray:
    """Extract Diffuse Horizontal Irradiance: DHI = SWDIFDS_RAD.

    Parameters
    ----------
    ds_diffuse : xarray.Dataset
        Dataset with the diffuse radiation field.
    diffuse_var : str
        Variable name.

    Returns
    -------
    xarray.DataArray
        DHI in W/m².
    """
    dhi = _resolve_var(ds_diffuse, diffuse_var).clip(min=0)
    dhi.attrs = {"units": "W/m2", "long_name": "Diffuse Horizontal Irradiance"}
    return dhi


def compute_albedo(
    ds_diffuse: xarray.Dataset,
    ds_direct: xarray.Dataset,
    ds_net: xarray.Dataset,
    diffuse_var: str = "SWDIFDS_RAD",
    direct_var: str = "SWDIRS_RAD",
    net_var: str = "SOBS_RAD",
) -> xarray.DataArray:
    """Compute surface albedo from net vs. incoming shortwave radiation.

    ``albedo = (GHI - SOBS_RAD) / GHI`` where ``GHI = SWDIRS_RAD +
    SWDIFDS_RAD`` (clipped to ``[0, inf)`` before summing, matching
    :func:`compute_ghi`) and ``SOBS_RAD`` is net shortwave at the
    surface (incoming minus reflected) -- **not** ``ASOB_S``, its
    average-type sibling (see ``downloaded_attributes.py``'s
    ``SOBS_RAD`` entry). Matches ERA5-Land's ``fal``/MERRA-2's
    ``ALBEDO`` canonical output name for cross-provider parity.

    Undefined at night (GHI -> 0, 0/0): cells where GHI is below a
    small W/m^2 floor are set to NaN rather than a spurious ratio --
    the same convention MERRA-2's native ``ALBEDO`` already exhibits
    (~47% NaN, all night hours; see ``.claude/merra2/merra2_plan.md``),
    so this isn't a new cross-provider inconsistency to document.

    Returns
    -------
    xarray.DataArray
        Albedo, dimensionless fraction, clipped to ``[0, 1]``; NaN at
        night.
    """
    diffuse = _resolve_var(ds_diffuse, diffuse_var)
    direct = _resolve_var(ds_direct, direct_var)
    net = _resolve_var(ds_net, net_var)

    ghi = ghi_from_diffuse_direct(diffuse, direct)
    albedo = ((ghi - net) / ghi).clip(0.0, 1.0)
    albedo = albedo.where(ghi > 1.0)  # night guard: undefined at GHI ~ 0
    albedo.attrs = {
        "units": "1",
        "long_name": "Surface albedo",
        "description": (
            "(GHI - SOBS_RAD) / GHI, where GHI = SWDIRS_RAD + "
            "SWDIFDS_RAD and SOBS_RAD is net shortwave at the surface. "
            "NaN at night (GHI <= 1 W/m^2)."
        ),
        "derivation": "ALBEDO = (GHI - SOBS_RAD) / GHI",
    }
    return albedo


def compute_snowfall(
    ds_con: xarray.Dataset,
    ds_gsp: xarray.Dataset,
    con_var: str = "SNOW_CON",
    gsp_var: str = "SNOW_GSP",
) -> xarray.DataArray:
    """Combine convective + stratiform snow into one SNOWFALL field.

    ``SNOWFALL = SNOW_CON + SNOW_GSP`` (both already accumulated mass
    over the hour, kg/m^2 == kg/m^2/h). Canonical name matches
    ERA5-Land's renamed ``sf``/MERRA-2's renamed ``PRECSNOLAND`` -- all
    three are the same physical quantity (accumulated mass of solid
    precipitation). Clipped to ``[0, inf)``: negative values are a
    GRIB artefact, not physically possible for either input.

    Returns
    -------
    xarray.DataArray
        Combined snowfall, kg/m^2/h.
    """
    con = _resolve_var(ds_con, con_var).clip(min=0)
    gsp = _resolve_var(ds_gsp, gsp_var).clip(min=0)
    snowfall = (con + gsp).rename("SNOWFALL")
    snowfall.attrs = {
        "units": "kg/m^2/h",
        "long_name": "Snowfall (accumulated mass, convective + stratiform)",
        "description": (
            "SNOW_CON (convective) + SNOW_GSP (stratiform), both "
            "accumulated over the hour."
        ),
        "derivation": "SNOWFALL = SNOW_CON + SNOW_GSP",
    }
    return snowfall


def compute_wind_speed(
    ds_u: xarray.Dataset,
    ds_v: xarray.Dataset,
    u_var: str = "u10",
    v_var: str = "v10",
) -> xarray.DataArray:
    """Compute scalar wind speed: WS = sqrt(U² + V²).

    The magnitude is identical in both the rotated-pole and WGS84
    coordinate systems, so no vector rotation is needed for this
    derived quantity.

    Parameters
    ----------
    ds_u, ds_v : xarray.Dataset
        Datasets with U and V wind components.
    u_var, v_var : str
        Variable names.

    Returns
    -------
    xarray.DataArray
        Wind speed at 10 m in m/s. Formula is
        :func:`~weather.common.derived_attributes.wind_speed` (single
        source of truth, shared with every provider).
    """
    xr = _import_xarray()
    u = _resolve_var(ds_u, u_var)
    v = _resolve_var(ds_v, v_var)
    ws: xarray.DataArray = xr.DataArray(
        wind_speed(u, v),
        coords=u.coords,
        dims=u.dims,
        attrs={"units": "m/s", "long_name": "Wind speed at 10m"},
    )
    return ws


def compute_dni(
    ds_direct: xarray.Dataset,
    *,
    elevation_threshold: float = DNI_ELEVATION_THRESHOLD_DEG,
) -> xarray.DataArray:
    """Compute per-cell Direct Normal Irradiance (DNI) in W/m².

    DNI = SWDIRS_RAD / cos(θ_z), where θ_z is the solar zenith angle,
    derived analytically from the grid's WGS84 latitude/longitude and
    UTC time using the Spencer (1971) formula.

    Cells where the solar elevation is below *elevation_threshold* are
    set to zero instead of dividing by a near-zero cos(θ_z).  This is
    physically correct — there is no direct beam below the horizon —
    and avoids the artificial inflation that a simple cos-clip produces
    at low sun angles.

    When the input dataset is dask-backed (the normal case after
    :func:`open_grib_month`), the entire SZA computation is performed
    lazily with ``dask.array``, chunked on **both** the time axis (168
    time steps per chunk) and the spatial axes (~206 × 212 cells per
    chunk).  This produces ≈ 80 independent tasks for a one-month
    824 × 848 grid, filling many cores on the dask threaded scheduler.

    .. note::
        **Why not pvlib?**  pvlib's ``get_solarposition`` is designed
        for ``(DatetimeIndex, scalar_lat, scalar_lon)`` — one location
        at a time.  For a 824 × 848 grid you would need ~700 k serial
        calls or a time loop, which is many times slower than a fully
        vectorised Spencer formula broadcast over the same grid.  pvlib
        is the right tool for single-site analysis or DISC decomposition
        from GHI; for rasterised grids, Spencer is preferred.

    .. warning::
        This is an **experimental** diagnostic intended for testing and
        exploration only.  Use GHI with a pvlib DISC decomposition for
        production-quality DNI estimates.

    Parameters
    ----------
    ds_direct : xarray.Dataset
        SWDIRS_RAD dataset from :func:`open_grib_month`.  Must have
        ``latitude`` and ``longitude`` as 2-D auxiliary coordinates
        (written automatically by cfgrib for COSMO-REA6 GRIBs).
    elevation_threshold : float
        Minimum solar elevation in degrees for which DNI is reported.
        Cells where the sun is below this angle are set to zero.
        Default is 5.0 (i.e. sun must be at least 5° above horizon).

    Returns
    -------
    xarray.DataArray
        DNI in W/m², same spatiotemporal shape as SWDIRS_RAD.
        Zero where solar elevation < *elevation_threshold*.

    Notes
    -----
    ``cos_sza_safe`` is clipped to ``[1e-3, 1.0]``.  The lower bound prevents
    division-by-zero at the horizon (dask evaluates both branches of
    ``xr.where`` before masking, so an ``inf`` in the un-masked branch
    would propagate into the output).  The upper bound prevents float32
    rounding in the Spencer dot-product from pushing ``cos(θ_z)`` above 1.0,
    which would give ``DNI < SWDIRS_RAD`` for above-threshold cells.

    References
    ----------
    Spencer, J. W. (1971). Fourier series representation of the position
    of the sun.  *Search*, 2(5), 172.
    """

    xr = _import_xarray()

    var_name = next(iter(ds_direct.data_vars))
    swdirs = ds_direct[var_name]

    if (
        "latitude" not in ds_direct.coords
        or "longitude" not in ds_direct.coords
    ):
        raise ValueError(
            "Dataset is missing 'latitude'/'longitude' coordinates. "
            "cfgrib writes these from COSMO-REA6 GRIBs automatically; "
            "verify the GRIB file contains a valid grid description."
        )

    # 2-D geographic coordinates (y, x)  [float32 saves memory]
    lat_2d = ds_direct["latitude"].values.astype("float32")
    lon_2d = ds_direct["longitude"].values.astype("float32")

    ts = pd.DatetimeIndex(swdirs.time.values)
    doy = ts.dayofyear.values.astype("float32")                    # (T,)
    utc_h = (ts.hour + ts.minute / 60.0).values.astype("float32")  # (T,)

    ny, nx = lat_2d.shape
    T = doy.shape[0]

    da_mod: Any = None
    try:
        import dask.array as da_mod
        _dask_backed = hasattr(swdirs.data, "__dask_graph__")
    except ImportError:
        _dask_backed = False

    m: Any
    if _dask_backed:
        _chunks = swdirs.chunks
        assert _chunks is not None
        time_chunks = _chunks[0]
        # Spatial chunks: quarter the grid (~206 × 212 for 824 × 848).
        # This gives T/168 × 4 × 4 ≈ 80 independent dask tasks, enough
        # to saturate many-core machines.  Increase the divisor to reduce
        # per-chunk memory; decrease it if the task graph overhead matters.
        y_chunk = max(1, ny // 4)
        x_chunk = max(1, nx // 4)
        mem_mb = T * ny * nx * 4 / (1024 * 1024)
        n_chunks = len(time_chunks) * ((ny + y_chunk - 1) // y_chunk) * (
            (nx + x_chunk - 1) // x_chunk
        )
        logger.info(
            "  DNI: %d×%d×%d float32 dask"
            " (time chunks %s, spatial %d×%d → %d tasks, ~%.0f MB)",
            T, ny, nx, time_chunks, y_chunk, x_chunk, n_chunks, mem_mb,
        )
        doy_bc = da_mod.from_array(
            doy[:, None, None], chunks=(time_chunks, 1, 1)
        )
        utc_h_bc = da_mod.from_array(
            utc_h[:, None, None], chunks=(time_chunks, 1, 1)
        )
        # Chunk lat/lon spatially so the broadcast result inherits chunks
        # on all three axes → full 3-D task graph.
        lat_bc = da_mod.from_array(
            lat_2d[None], chunks=(1, y_chunk, x_chunk)
        )
        lon_bc = da_mod.from_array(
            lon_2d[None], chunks=(1, y_chunk, x_chunk)
        )
        m = da_mod
    else:
        mem_mb = T * ny * nx * 4 / (1024 * 1024)
        logger.info(
            "  DNI: %d×%d×%d float32 numpy (~%.0f MB)", T, ny, nx, mem_mb,
        )
        doy_bc = doy[:, None, None]
        utc_h_bc = utc_h[:, None, None]
        lat_bc = lat_2d[None]
        lon_bc = lon_2d[None]
        m = np

    # Spencer (1971): day-angle, declination, equation of time, hour angle
    B = (2.0 * np.pi / 365.0) * (doy_bc - 1.0)

    # Solar declination (radians)
    decl = (
        0.006918
        - 0.399912 * m.cos(B) + 0.070257 * m.sin(B)
        - 0.006758 * m.cos(2*B) + 0.000907 * m.sin(2*B)
        - 0.002697 * m.cos(3*B) + 0.001480 * m.sin(3*B)
    )

    # Equation of time (hours)
    eot = (
        0.000075
        + 0.001868 * m.cos(B) - 0.032077 * m.sin(B)
        - 0.014615 * m.cos(2*B) - 0.040890 * m.sin(2*B)
    ) * (229.18 / 60.0)

    # Hour angle (radians): 15°/h, longitude + EoT correction
    hour_angle = (
        15.0 * (utc_h_bc + lon_bc / 15.0 + eot - 12.0)
    ) * (np.pi / 180.0)
    lat_rad = lat_bc * (np.pi / 180.0)

    # cos(zenith) — unclipped, used for elevation masking below
    cos_sza_raw = (
        m.sin(lat_rad) * m.sin(decl)
        + m.cos(lat_rad) * m.cos(decl) * m.cos(hour_angle)
    )

    # Solar elevation in degrees; arcsin input clamped to [-1, 1] for safety
    elevation = (
        m.arcsin(m.clip(cos_sza_raw, -1.0, 1.0)) * (180.0 / np.pi)
    )   # degrees, shape (T, y, x)

    # Clip to [1e-3, 1.0]: lower → finite division at horizon;
    # upper → corrects float32 rounding above 1.0 (see docstring Notes).
    cos_sza_safe = m.clip(cos_sza_raw, 1e-3, 1.0)

    cos_da: xarray.DataArray = xr.DataArray(cos_sza_safe, dims=swdirs.dims)
    elev_da: xarray.DataArray = xr.DataArray(elevation, dims=swdirs.dims)

    # Formula (division + threshold zero-out) is
    # derived_attributes.dni_from_direct -- single source of truth,
    # also used by derived_attributes._cosmo_dni. cos_da/elev_da are
    # already bounded above ([1e-3, 1.0] / Spencer elevation), so no
    # extra work happens here versus the previous inline version.
    dni: xarray.DataArray = dni_from_direct(
        swdirs, cos_da, elev_da, elevation_threshold,
    ).rename("DNI")

    dni.attrs = {
        "long_name": "Direct Normal Irradiance (experimental per-cell)",
        "units": "W/m2",
        "note": (
            f"DNI = SWDIRS_RAD / cos(SZA) where elevation >= "
            f"{elevation_threshold} deg, else 0. "
            "SZA from Spencer (1971). "
            "Experimental — use pvlib DISC from GHI for production DNI."
        ),
    }
    logger.info(
        "  DNI computed; elevation threshold %.1f deg"
        " (%s backend)",
        elevation_threshold,
        "dask" if _dask_backed else "numpy",
    )
    return dni


def log_dni_stats(dni: xarray.DataArray, ds_direct: xarray.Dataset) -> None:
    """Log DNI distribution stats and consistency checks (informational only).

    Samples every 24th timestep (~1 snapshot/day) to keep memory low.
    Does not modify any data or raise exceptions.

    Notes on expected value ranges
    ------------------------------
    Negative DNI is structurally impossible: SWDIRS_RAD >= 0 always (it is
    an irradiance from the CR6 reanalysis model) and cos_sza is clipped to
    a small positive value, so the division cannot produce a negative
    result.

    Checks logged
    -------------
    - SWDIRS = 0 -> DNI = 0 (consistency between input and output).
    - DNI >= SWDIRS_RAD for all daytime cells (cos_sza <= 1, so dividing
      by it can only increase or preserve the value).
    - Percentile distribution of daytime-only DNI values.
    - Zero fraction (= night + below-elevation-threshold fraction).
    """
    logger.info("  DNI stats (daily sample) ...")

    raw_name = next(iter(ds_direct.data_vars))
    swdirs = ds_direct[raw_name]

    stride = max(1, len(dni.time) // 31)
    try:
        dni_s = (
            dni.isel(time=slice(None, None, stride))
            .values.ravel().astype("float32")
        )
        sw_s = (
            swdirs.isel(time=slice(None, None, stride))
            .values.ravel().astype("float32")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("  DNI stats skipped — could not compute sample: %s", exc)
        return

    n_total = len(dni_s)

    # Consistency: SWDIRS = 0 → DNI must be 0
    mask_night = sw_s <= 0.0
    n_bad = int((mask_night & (dni_s > 0.1)).sum())
    if n_bad > 0:
        logger.warning(
            "  SWDIRS=0 but DNI>0: %d cells — check CR6 data or formula",
            n_bad,
        )
    else:
        logger.info("  SWDIRS=0 → DNI=0  (consistent)")

    # Consistency: DNI ≥ SWDIRS for cells where DNI > 0 (cos_sza ≤ 1 always).
    # We exclude DNI = 0 cells because the elevation threshold deliberately
    # zeros DNI when sun elevation < 5°, even if SWDIRS_RAD is small but
    # non-zero (CR6 assigns tiny SWDIRS values at grazing incidence).
    # Those zeroed cells are correct behaviour, not a formula error.
    mask_day = sw_s > 1.0
    n_day = int(mask_day.sum())
    mask_active = mask_day & (dni_s > 0.0)   # sun above threshold
    n_active = int(mask_active.sum())
    n_zeroed = n_day - n_active              # correctly zeroed by threshold
    if n_zeroed > 0:
        logger.info(
            "  %d daytime cells zeroed by elevation threshold (correct)",
            n_zeroed,
        )
    if n_active > 0:
        n_bad2 = int((mask_active & (dni_s < sw_s * 0.99)).sum())
        if n_bad2 > 0:
            logger.warning(
                "  DNI < SWDIRS_RAD: %d / %d above-threshold cells"
                " — unexpected (cos_sza clamped to 1.0, check formula)",
                n_bad2, n_active,
            )
        else:
            logger.info(
                "  DNI >= SWDIRS_RAD for all %d above-threshold cells",
                n_active,
            )

    # Distribution
    pct_zero = 100.0 * float((dni_s == 0.0).sum()) / n_total
    logger.info("DNI zero fraction (night + below-threshold): %.1f%%", pct_zero)

    dni_day = dni_s[mask_day]
    if len(dni_day) > 0:
        logger.info(
            "DNI daytime percentiles (n=%d):"
            "p25=%.0f  p50=%.0f  p75=%.0f  p95=%.0f  p99=%.0f  max=%.0f  W/m²",
            len(dni_day),
            float(np.percentile(dni_day, 25)),
            float(np.percentile(dni_day, 50)),
            float(np.percentile(dni_day, 75)),
            float(np.percentile(dni_day, 95)),
            float(np.percentile(dni_day, 99)),
            float(dni_day.max()),
        )


def report_dni_outliers(nc_path: Path, threshold: float = 1400.0) -> None:
    """Open the saved NetCDF and report cells where DNI exceeds *threshold*.

    Finds all grid cells whose peak DNI across all timesteps is >=
    threshold, then logs their latitude, longitude, grid indices (y, x),
    and peak value. At most 20 cells are listed individually; the total
    count is always shown.

    Parameters
    ----------
    nc_path : Path
        Path to the exported NetCDF file.
    threshold : float
        W/m² above which a cell is considered an outlier. Default 1400
        W/m². The solar constant is ~1361 W/m² at TOA; surface DNI cannot
        reach this value, so any cell >= 1400 W/m² indicates unphysical
        CR6 data or a formula error in :func:`compute_dni`.
    """
    xr = _import_xarray()

    logger.info("=" * 68)
    logger.info("DNI OUTLIER REPORT  (threshold >= %.0f W/m²)", threshold)

    with xr.open_dataset(nc_path) as ds:
        if "DNI" not in ds:
            logger.info("DNI not present in output file (--skip-dni was used)")
            return

        dni = ds["DNI"]

        # Peak DNI at each grid cell across all timesteps → shape (y, x)
        dni_peak = dni.max(dim="time").values.astype("float32")
        mask = dni_peak >= threshold
        n_cells = int(mask.sum())
        total_cells = int(mask.size)

        if n_cells == 0:
            logger.info(
                "  No cells with DNI >= %.0f W/m²  "
                "(all values within physical range)",
                threshold,
            )
            return

        pct = 100.0 * n_cells / total_cells
        logger.warning(
            "  %d / %d grid cells (%.4f%%) have peak DNI >= %.0f W/m²",
            n_cells, total_cells, pct, threshold,
        )

        has_coords = "latitude" in ds.coords and "longitude" in ds.coords
        if has_coords:
            lat2d = ds["latitude"].values
            lon2d = ds["longitude"].values

        ys, xs = np.where(mask)
        vals = dni_peak[mask]
        order = np.argsort(vals)[::-1][:20]

        logger.warning("  Top %d cells by peak DNI:", min(20, n_cells))
        for rank, idx in enumerate(order):
            y, x = int(ys[idx]), int(xs[idx])
            peak = float(vals[idx])
            if has_coords:
                logger.warning(
                    "    #%02d  lat=%7.3f  lon=%7.3f"
                    "  (y=%d, x=%d)  peak=%.1f W/m²",
                    rank + 1, float(lat2d[y, x]), float(lon2d[y, x]),
                    y, x, peak,
                )
            else:
                logger.warning(
                    "    #%02d  y=%d  x=%d  peak=%.1f W/m²",
                    rank + 1, y, x, peak,
                )

        if n_cells > 20:
            logger.warning("  ... and %d more cells (not listed)", n_cells - 20)

    logger.info("=" * 68)


def _strip_scalar_coords(da: xarray.DataArray) -> xarray.DataArray:
    """Drop all non-dimension scalar coordinates from a DataArray.

    cfgrib attaches scalar coordinates like ``heightAboveGround``, ``step``,
    ``surface``, and ``valid_time`` to each dataset.  These values differ
    across variables (e.g. T_2M has ``heightAboveGround=2``, wind has
    ``heightAboveGround=10``, radiation has none) and cause
    ``MergeError: conflicting values`` when DataArrays are combined into a
    single :class:`xarray.Dataset`.

    Dropping these non-dimension coordinates is safe because the information
    is already captured in the variable names and metadata.
    """
    drop = [c for c in da.coords if c not in da.dims]
    return da.drop_vars(drop) if drop else da


# Mapping from COSMO-REA6 canonical names to the eccodes/cfgrib short names
# that cfgrib may assign when decoding GRIBs.  Each CR6 per-attribute GRIB
# contains exactly one variable, so cfgrib's internal renaming does not
# affect which data are returned — only which name they appear under.
_CFGRIB_ALIASES: dict[str, tuple[str, ...]] = {
    "PS":         ("sp",),
    "H_SNOW":     ("sde", "sd"),
    "SNOW_GSP":   ("lssf", "sf"),
    "SNOW_CON":   ("snoc", "csf"),
    "T_2M":       ("2t", "t2m"),
    "U_10M":      ("10u", "u10"),
    "V_10M":      ("10v", "v10"),
    "SWDIRS_RAD": ("ssrd", "fdir"),
    "SWDIFDS_RAD": ("sdiff", "ssrdc"),
}


def _resolve_var(ds: xarray.Dataset, preferred: str) -> xarray.DataArray:
    """Resolve a variable from a dataset, trying common cfgrib aliases.

    cfgrib decodes COSMO GRIB variables using the eccodes short-name table,
    which may differ from the COSMO-REA6 shorthand names (e.g. 'PS' becomes
    'sp', 'H_SNOW' becomes 'sde').  This helper tries the caller's preferred
    name first, then any known cfgrib aliases, then the first data variable
    (safe because each per-attribute GRIB file contains exactly one variable).

    Parameters
    ----------
    ds : xarray.Dataset
    preferred : str
        Preferred variable name (COSMO-REA6 shorthand).

    Returns
    -------
    xarray.DataArray

    Raises
    ------
    KeyError
        If the dataset has no data variables.
    """
    if preferred in ds:
        return ds[preferred]

    # Try lowercase match
    lower = preferred.lower()
    for name in ds.data_vars:
        if str(name).lower() == lower:
            return ds[name]

    # Try known cfgrib eccodes aliases (no warning — expected behaviour)
    for alias in _CFGRIB_ALIASES.get(preferred, ()):
        if alias in ds:
            logger.debug(
                "'%s' decoded by cfgrib as '%s' (eccodes alias)",
                preferred, alias,
            )
            return ds[alias]

    # Last resort: take the only variable in the file (each GRIB is
    # per-attribute, so there is exactly one data variable)
    data_vars = list(ds.data_vars)
    if data_vars:
        logger.debug(
            "'%s' not found by name; using sole data variable '%s'",
            preferred, data_vars[0],
        )
        return ds[data_vars[0]]

    raise KeyError(f"No data variables in dataset; expected '{preferred}'")


# ---------------------------------------------------------------------------
# Full annual dataset assembly
# ---------------------------------------------------------------------------

def build_annual_dataset(
    year: int | None = None,
    *,
    grb_dir: Path | None = None,
    include_wind_components: bool = True,
) -> xarray.Dataset:
    """Assemble a complete annual dataset with all derived variables.

    Reads all five raw attributes for all twelve months, applies unit
    conversions, computes GHI/DHI/WS_10M/T, and merges into a single
    :class:`xarray.Dataset`.

    Parameters
    ----------
    year : int, optional
        Target year (default from config).
    grb_dir : Path, optional
        Root decompressed-GRIB directory.
    include_wind_components : bool
        If ``True`` (default), keep raw ``U_10M`` and ``V_10M`` in the
        output alongside the scalar ``WS_10M``.  A metadata warning is
        attached noting that U/V are in rotated-pole coordinates.

    Returns
    -------
    xarray.Dataset
        Variables: ``T``, ``GHI``, ``DHI``, ``WS_10M``, and optionally
        ``U_10M``, ``V_10M``.  Coordinates include the native rotated-pole
        grid dims (``y``, ``x``) and, when cfgrib decodes them from the
        source GRIBs (the normal case), auxiliary 2-D WGS84 ``latitude``/
        ``longitude`` coordinates — see :func:`find_nearest_cell`.

    Notes
    -----
    DNI is not included in the output of this function.  Use
    :func:`compute_dni` on the individual monthly datasets if per-grid-cell
    DNI is needed, or apply a pvlib DISC decomposition from GHI for
    single-site analysis.
    """
    xr = _import_xarray()
    from .config import get_config

    cfg = get_config()
    year = year or cfg["year"]

    ncores = cfg["ncores"]
    # Threaded scheduler: all threads share one process address space,
    # which is more memory-efficient than multi-process for large arrays.
    import dask
    dask.config.set(scheduler="threads", num_workers=ncores)
    logger.info("Dask threaded scheduler: %d threads", ncores)

    logger.info("Building annual dataset for %d", year)

    # Load all 5 raw attributes in parallel — cfgrib I/O is the slowest
    # part, and each attribute reads independent files on disk.
    attrs_to_load = ["SWDIFDS_RAD", "SWDIRS_RAD", "T_2M", "U_10M", "V_10M"]

    def _load(attr: str) -> xarray.Dataset:
        return open_grib_year(attr, year, grb_dir=grb_dir)

    logger.info("Reading %d attributes in parallel (%d threads)...",
                len(attrs_to_load), min(len(attrs_to_load), ncores))
    n_workers = min(len(attrs_to_load), ncores)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        results = list(pool.map(_load, attrs_to_load))

    ds_diffuse, ds_direct, ds_temp, ds_u, ds_v = results

    # Capture the real per-cell WGS84 coordinates cfgrib already decoded
    # from the GRIB grid definition, before the derived-variable strip
    # below drops them (they are auxiliary, non-dimension coordinates on
    # ds_direct itself, untouched by that strip). Re-attached to the
    # final dataset below so point_query.find_nearest_cell can do a real
    # nearest-neighbor search instead of an approximate rotated-pole
    # reconstruction.
    lat_2d: np.ndarray | None
    lon_2d: np.ndarray | None
    if "latitude" in ds_direct.coords and "longitude" in ds_direct.coords:
        lat_2d = ds_direct["latitude"].values
        lon_2d = ds_direct["longitude"].values
    else:
        lat_2d = lon_2d = None
        logger.warning(
            "SWDIRS_RAD dataset missing latitude/longitude coordinates; "
            "output will not carry per-cell coordinates."
        )

    # Derive fields
    T = convert_temperature(ds_temp)
    GHI = compute_ghi(ds_diffuse, ds_direct)
    DHI = compute_dhi(ds_diffuse)
    WS = compute_wind_speed(ds_u, ds_v)

    # Strip conflicting non-dimension scalar coordinates (e.g.
    # heightAboveGround, step, surface) before merging — cfgrib attaches
    # different values for different GRIB sources which causes MergeError.
    T = _strip_scalar_coords(T)
    GHI = _strip_scalar_coords(GHI)
    DHI = _strip_scalar_coords(DHI)
    WS = _strip_scalar_coords(WS)

    # Assemble output dataset
    out = xr.Dataset(
        {
            "T": T,
            "GHI": GHI,
            "DHI": DHI,
            "WS_10M": WS,
        },
        attrs={
            "title": f"COSMO-REA6 processed weather data — {year}",
            "source": (
                "DWD COSMO-REA6 reanalysis "
                "(https://opendata.dwd.de/climate_environment/REA/COSMO_REA6/)"
            ),
            "spatial_resolution": "0.055° (~6 km)",
            "temporal_resolution": "1 hour",
            "processing": "buem.weather pipeline",
            "conventions": "CF-1.8",
            "note_DNI": (
                "DNI is not included in this file. "
                "Use compute_dni() on individual monthly datasets for "
                "per-grid-cell DNI, or apply pvlib DISC decomposition "
                "from GHI for single-site analysis."
            ),
        },
    )

    if lat_2d is not None:
        out = out.assign_coords(
            latitude=(("y", "x"), lat_2d),
            longitude=(("y", "x"), lon_2d),
        )
        attach_cf_latlon_attrs(out)

    if include_wind_components:
        u_raw = _strip_scalar_coords(_resolve_var(ds_u, "u10"))
        v_raw = _strip_scalar_coords(_resolve_var(ds_v, "v10"))
        u_raw.attrs["long_name"] = (
            "U-component of wind at 10m (rotated-pole grid north)"
        )
        u_raw.attrs["warning"] = (
            "This field uses COSMO-REA6 rotated-pole grid coordinates.  "
            "For true-north wind direction, apply the rotated-pole → WGS84 "
            "vector rotation using pyproj or the COSMO rotation angle."
        )
        v_raw.attrs["long_name"] = (
            "V-component of wind at 10m (rotated-pole grid north)"
        )
        v_raw.attrs["warning"] = u_raw.attrs["warning"]
        out["U_10M"] = u_raw
        out["V_10M"] = v_raw

    logger.info(
        "Annual dataset assembled: %d variables, %d timesteps",
        len(out.data_vars), out.sizes.get("time", 0),
    )
    return out


def _passthrough_var(
    datasets: dict[str, xarray.Dataset], attr: str,
) -> xarray.DataArray:
    """Build one output :class:`~xarray.DataArray` for a
    ``role: "passthrough"`` attribute (see
    :data:`~weather.providers.cosmo_rea6.downloaded_attributes.ATTRIBUTES`)
    using only its registered ``unit_target``/``description`` -- no
    per-variable code. Not for ``role: "formula"`` attributes (T_2M,
    SWDIFDS_RAD, SWDIRS_RAD, U_10M, V_10M), which need real derivations.
    """
    meta = ATTRIBUTES[attr]
    name = canonical_name(attr)
    da = _strip_scalar_coords(_resolve_var(datasets[attr], attr)).rename(name)
    da.attrs.update({
        "units": meta["unit_target"],
        "long_name": meta["description"],
    })
    return da


def build_month_dataset(
    year: int,
    month: int,
    *,
    grb_dir: Path | None = None,
    compute_dni_field: bool = True,
    crop_bbox: BBox | None = None,
) -> tuple[xarray.Dataset, dict[str, xarray.Dataset]]:
    """Assemble one month's full COSMO-REA6 output dataset.

    The single place that turns every attribute in
    :data:`~weather.providers.cosmo_rea6.downloaded_attributes.ATTRIBUTES`
    into an output variable: T/GHI/DHI/WS_10M via the explicit formulas
    below (``role: "formula"``), every other registered attribute via
    :func:`_passthrough_var` (``role: "passthrough"``). Adding or
    removing a passthrough attribute in ``downloaded_attributes.py``
    changes the next run's output automatically -- no edits needed here
    or in ``tests/test_cosmo_one_month.py`` / ``tests/test_cosmo_one_year.py``,
    which both call this function instead of assembling the dataset
    themselves (previously two independent, hand-duplicated copies of
    this same assembly logic).

    Parameters
    ----------
    year, month : int
        Target period.
    grb_dir : Path, optional
        Root decompressed-GRIB directory (default from config).
    compute_dni_field : bool
        If ``True`` (default), also compute the experimental per-cell
        DNI (see :func:`compute_dni`). Set ``False`` to skip it (e.g.
        ``--skip-dni``).
    crop_bbox : BBox, optional
        If given, crop every attribute to this bbox's ``(y, x)`` index
        window (see :mod:`.crop`) immediately after opening, before any
        derived-field computation -- default ``None`` means the crop
        module is never imported or called at all (hard no-op, not just
        a trivial pass-through).

    Returns
    -------
    tuple[xarray.Dataset, dict[str, xarray.Dataset]]
        The assembled month dataset (carrying auxiliary 2-D WGS84
        ``latitude``/``longitude`` coordinates when cfgrib decodes them
        from the source GRIBs — see :func:`find_nearest_cell`), and the
        raw per-attribute datasets (needed by callers for DNI
        outlier/stats logging against the raw ``SWDIRS_RAD`` field).
    """
    xr = _import_xarray()

    datasets = {
        attr: open_grib_month(attr, year, month, grb_dir=grb_dir)
        for attr in ATTRIBUTES
    }

    if crop_bbox is not None:
        from .crop import compute_crop_index, crop_datasets

        y_slice, x_slice = compute_crop_index(
            crop_bbox, datasets["SWDIRS_RAD"]
        )
        datasets = crop_datasets(datasets, y_slice, x_slice)

    # See build_annual_dataset for why this is captured before the
    # derived-variable strip below.
    _swdirs = datasets["SWDIRS_RAD"]
    lat_2d: np.ndarray | None
    lon_2d: np.ndarray | None
    if "latitude" in _swdirs.coords and "longitude" in _swdirs.coords:
        lat_2d = _swdirs["latitude"].values
        lon_2d = _swdirs["longitude"].values
    else:
        lat_2d = lon_2d = None
        logger.warning(
            "SWDIRS_RAD dataset missing latitude/longitude coordinates; "
            "output will not carry per-cell coordinates."
        )

    T = _strip_scalar_coords(convert_temperature(datasets["T_2M"]))
    GHI = _strip_scalar_coords(
        compute_ghi(datasets["SWDIFDS_RAD"], datasets["SWDIRS_RAD"])
    )
    DHI = _strip_scalar_coords(compute_dhi(datasets["SWDIFDS_RAD"]))
    WS = _strip_scalar_coords(
        compute_wind_speed(datasets["U_10M"], datasets["V_10M"])
    )
    ALBEDO = _strip_scalar_coords(
        compute_albedo(
            datasets["SWDIFDS_RAD"], datasets["SWDIRS_RAD"],
            datasets["SOBS_RAD"],
        )
    )
    SNOWFALL = _strip_scalar_coords(
        compute_snowfall(datasets["SNOW_CON"], datasets["SNOW_GSP"])
    )
    T_DEW = _strip_scalar_coords(compute_dewpoint(T, datasets["RELHUM_2M"]))
    # Raw 10 m wind components, kept alongside the derived WS_10M scalar
    # -- matches ERA5-Land/MERRA-2, which both keep their raw U_10M/V_10M
    # too (this was a real cross-provider inconsistency: COSMO used to
    # be the only one of the three that discarded them). role="formula"
    # in ATTRIBUTES (feeds WS_10M) so the generic passthrough_attrs()
    # loop below skips these; call _passthrough_var directly instead --
    # it only reads dict metadata, doesn't care about role.
    U_10M = _passthrough_var(datasets, "U_10M")
    V_10M = _passthrough_var(datasets, "V_10M")

    data_vars: dict[str, xarray.DataArray] = {
        "T": T, "GHI": GHI, "DHI": DHI, "WS_10M": WS, "ALBEDO": ALBEDO,
        "SNOWFALL": SNOWFALL, "T_DEW": T_DEW,
        "U_10M": U_10M, "V_10M": V_10M,
    }
    for attr in passthrough_attrs():
        da = _passthrough_var(datasets, attr)
        data_vars[str(da.name)] = da

    if compute_dni_field:
        DNI = _strip_scalar_coords(compute_dni(datasets["SWDIRS_RAD"]))
        data_vars["DNI"] = DNI

    ds_out = xr.Dataset(data_vars)
    if lat_2d is not None:
        ds_out = ds_out.assign_coords(
            latitude=(("y", "x"), lat_2d),
            longitude=(("y", "x"), lon_2d),
        )
        attach_cf_latlon_attrs(ds_out)
    logger.info(
        "Month dataset assembled: %d variables, %d timesteps",
        len(ds_out.data_vars), ds_out.sizes.get("time", 0),
    )
    return ds_out, datasets


# Re-exported here for backward-compatible import convenience within the
# pipeline; the actual implementation lives in weather.common.geo_lookup
# (pure numpy, no xarray/cfgrib) so weather.point_query — and anything
# else that only needs a nearest-cell lookup — can use it without
# triggering this package's heavy __init__.py import chain.
from ...common.geo_lookup import find_nearest_cell  # noqa: E402,F401
