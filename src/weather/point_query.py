"""Single-location weather lookup from already-processed provider archives.

This module does **not** download or process data itself — it only opens
NetCDF files that a prior ``weather run --provider ... --year ...`` (or
equivalent pipeline call) already produced, and extracts the nearest grid
cell's requested variables for one ``(latitude, longitude, year)`` -- see
``weather.variables`` for the full registry (solar: ``T``/``GHI``/
``DHI``/``DNI``, wind: ``WS_10M``/``U_10M``/``V_10M``). *variables*/
*use_case* has no default -- exactly one is required. This keeps it
import-light: only ``numpy``/``pandas``/``xarray``/``netcdf4`` (the
``pointquery`` extra) plus ``pvlib`` (the ``solar`` extra, only actually
imported when GHI/DHI/DNI are requested) for DNI/DHI reconstruction —
never the GRIB/download/transform pipeline stack (``cfgrib``, ``dask``,
``eccodes``, ``pyproj`` — the ``pipeline`` extra).

Typical usage::

    from weather import get_point_weather

    df = get_point_weather(52.0, 5.0, 2018, provider="era5-land", use_case="solar")
    # df: DatetimeIndex, columns T (degC), GHI/DHI/DNI (W/m2)

    wind = get_point_weather(52.0, 5.0, 2018, provider="era5-land", use_case="wind")
    # df: DatetimeIndex, columns WS_10M/U_10M/V_10M (m/s)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from . import errors
from .common.dni_reconstruction import reconstruct_dni_dhi
from .common.env import data_root
from .common.geo_lookup import find_nearest_cell
from .errors import WeatherAPIError
from .variables import resolve_variables

logger = logging.getLogger(__name__)

_SOLAR_DERIVED = ("GHI", "DHI", "DNI")
_WIND_VARS = ("WS_10M", "U_10M", "V_10M")

_ALIASES: dict[str, str] = {
    "cosmo": "cosmo-rea6",
    "cosmo-rea6": "cosmo-rea6",
    "merra2": "merra-2",
    "merra-2": "merra-2",
    "era5": "era5-land",
    "era5-land": "era5-land",
}

_OUTPUT_SUBDIR: dict[str, str] = {
    "cosmo-rea6": "cosmo_rea6",
    "era5-land": "era5_land",
    "merra-2": "merra2",
}

def _import_xarray() -> Any:
    """Lazy-import xarray (the `pointquery` extra, not needed at import time)."""
    try:
        import xarray as xr

        return xr
    except ImportError as exc:
        raise ImportError(
            "xarray and netcdf4 are required for weather.get_point_weather(). "
            "Install with: pip install weather[pointquery]"
        ) from exc


def _output_dir(canonical: str, data_dir: Path | str | None) -> Path:
    """Resolve the directory containing a provider's already-exported files.

    Mirrors each provider's own ``<work_dir>/output`` convention
    (``EnvSettings.<provider>_output_dir()``) without importing the
    provider package itself — importing e.g. ``weather.providers.era5_land``
    would execute that package's ``__init__.py``, which imports its heavy
    pipeline module. *data_dir*, if given, overrides this entirely.
    """
    if data_dir is not None:
        return Path(data_dir)
    return data_root() / _OUTPUT_SUBDIR[canonical] / "output"


def _resolve_country_dir(
    canonical: str, latitude: float, longitude: float, year: int
) -> Path | None:
    """Best-effort lookup of a country-scoped output directory for
    *(latitude, longitude)*, following the ``<provider>/<country>/output``
    layout (see ``docs/COUNTRY_SCOPED_ARCHIVES.md``).

    Returns ``None`` -- meaning "fall through to the provider's flat
    default directory" -- whenever nothing more specific is available:
    no country's bounding box contains the point, or the matching
    country has no archive for *year* yet. This is an optimization over
    the default directory, never a requirement; every caller still gets
    a normal (or FileNotFoundError) result either way.

    Country bounding boxes are simple rectangles (see
    ``geo.countries.COUNTRIES``), so two neighbouring countries can
    overlap near a shared border; the first match (dict iteration order)
    wins. Fine for a handful of countries -- revisit with a proper
    nearest-centroid tiebreak if this list grows large enough for
    overlaps to matter often.
    """
    from .geo.countries import COUNTRIES

    provider_root = data_root() / _OUTPUT_SUBDIR[canonical]
    for country, bbox in COUNTRIES.items():
        if not (bbox.south <= latitude <= bbox.north and bbox.west <= longitude <= bbox.east):
            continue
        candidate = provider_root / country / "output"
        if candidate.is_dir() and any(candidate.glob(f"*{year}*")):
            return candidate
    return None


def _finalize(df: pd.DataFrame, variables: tuple[str, ...]) -> pd.DataFrame:
    """Normalize the index to tz-naive and select exactly *variables*, in
    the order requested."""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    df = df.copy()
    df.index = idx
    return df[list(variables)]


def _strip_tz(series: pd.Series) -> pd.Series:
    """Return *series* with a tz-naive index, converting if necessary.

    ``reconstruct_dni_dhi`` localizes a tz-naive GHI index to UTC on its
    own internal copy before computing DNI/DHI, so its returned Series can
    come back tz-**aware** even when the caller's GHI/T Series (straight
    off the NetCDF ``time`` coordinate) are tz-naive. Combining tz-naive
    and tz-aware Series in one ``pd.DataFrame({...})`` aligns on the
    union of their indices — which never match between naive and aware
    timestamps — silently producing all-NaN columns. Normalize before
    combining, not after.
    """
    idx = pd.DatetimeIndex(series.index)
    if idx.tz is not None:
        series = series.copy()
        series.index = idx.tz_convert(None)
    return series


def _temperature_series(cell: Any, legacy_name: str) -> pd.Series:
    """Return the Celsius temperature Series, tolerating pre-fix archives.

    Both ERA5-Land and MERRA-2 now export a canonical ``T`` column
    (matching COSMO-REA6); archives produced before that rename carry the
    raw provider name instead (already Celsius, just not renamed).
    """
    if "T" in cell:
        return cell["T"].to_series()
    return cell[legacy_name].to_series()


def _regular_grid_preprocess(
    legacy_temperature_var: str,
    legacy_pressure_var: str,
    unrepaired: list[str],
):
    """Build a per-file normalization hook, applied to each monthly file
    right after it is opened, BEFORE its cell is extracted.

    Two real, independent hazards show up in a multi-month archive that
    is mid-migration (some months regenerated, some not):

    1. **Split naming.**  A file's legacy variable name (e.g. MERRA-2's
       ``T2M``) is renamed to its canonical name (``T``) here whenever
       the canonical name is absent, so every month contributes to the
       SAME merged variable.  Doing this on the already-merged dataset
       (the previous approach) instead creates two separate variables
       when different months use different names — e.g. a real MERRA-2
       2018 archive where only March was regenerated with ``T``: the
       other 11 months' ``T2M`` merges in as a second variable, and
       ``cell["T"]`` silently resolves to the mostly-NaN one (populated
       only for March's 744 hours) instead of raising.
    2. **Unrepaired ERA5-Land boundary artifact.**  ``transform.py``
       deliberately leaves each month's FIRST hourly ``GHI``/``sf`` stamp
       as the raw accumulated daily total (not a real flux) until
       :func:`weather.providers.era5_land.boundary_repair.
       repair_boundaries` runs across the output folder (now automatic —
       see that module's docstring and ``pipeline.py``'s STEP 3/3) — an
       implausible multi-thousand W/m^2 spike if read directly.  Flag any
       file whose ``boundary_status`` doesn't confirm repair; MERRA-2
       files never set this attribute, so the check is a no-op for that
       provider.
    """

    def _preprocess(ds: Any) -> Any:
        status = str(ds.attrs.get("boundary_status", ""))
        if status and not status.startswith("BOUNDARY_REPAIRED"):
            source = ds.encoding.get("source", "<unknown file>")
            unrepaired.append(Path(source).name)

        renames = {}
        if "T" not in ds.variables and legacy_temperature_var in ds.variables:
            renames[legacy_temperature_var] = "T"
        if "PS" not in ds.variables and legacy_pressure_var in ds.variables:
            renames[legacy_pressure_var] = "PS"
        return ds.rename(renames) if renames else ds

    return _preprocess


def _get_point_regular_grid(
    latitude: float,
    longitude: float,
    year: int,
    data_dir: Path | str | None,
    variables: tuple[str, ...],
    *,
    canonical: str,
    filename_glob: str,
    legacy_pressure_var: str,
    legacy_temperature_var: str,
) -> pd.DataFrame:
    """Shared point extraction for ERA5-Land/MERRA-2's regular lat/lon grid.

    Opens each monthly file independently via ``xr.open_dataset`` rather
    than ``xr.open_mfdataset`` — point_query's whole reason to exist is
    extracting one cell from an already-processed archive without pulling
    in the heavy ``pipeline`` extra's dependencies (the ``pointquery``
    extra in ``pyproject.toml`` is deliberately scoped to xarray/netcdf4
    only). ``open_mfdataset`` looks read-only but always needs the dask
    chunk manager internally: it calls ``open_dataset(path, chunks={})``
    per file even when the caller never passed a ``chunks=`` argument,
    and a non-``None`` ``chunks`` forces dask-backed arrays — a genuine
    ``ImportError`` under a clean ``pip install weather[pointquery]``,
    not merely a style preference.
    """
    xr = _import_xarray()
    out_dir = _output_dir(canonical, data_dir)
    paths = sorted(out_dir.glob(filename_glob))
    if not paths:
        raise FileNotFoundError(
            f"No processed {canonical} files matching {filename_glob!r} "
            f"under {out_dir}. Run the {canonical} pipeline for {year} "
            "first (weather run --provider "
            f"{canonical} --year {year})."
        )

    need_solar = any(v in _SOLAR_DERIVED for v in variables)
    need_t = "T" in variables
    wind_vars = [v for v in variables if v in _WIND_VARS]

    unrepaired: list[str] = []
    preprocess = _regular_grid_preprocess(
        legacy_temperature_var, legacy_pressure_var, unrepaired
    )
    ghi_parts: list[pd.Series] = []
    t_parts: list[pd.Series] = []
    ps_parts: list[pd.Series] = []
    wind_parts: dict[str, list[pd.Series]] = {v: [] for v in wind_vars}
    cell_lat, cell_lon = latitude, longitude
    for p in paths:
        with xr.open_dataset(str(p)) as raw:
            ds = preprocess(raw)
            if "latitude" not in ds.coords or "longitude" not in ds.coords:
                raise KeyError(
                    f"{p.name} has no latitude/longitude coordinates (dims "
                    f"only: {list(ds.dims)}); it predates {canonical}'s "
                    "coordinate-retention convention. Re-run the pipeline's "
                    "transform+export phase for this file to regenerate it "
                    "with per-cell coordinates."
                )
            cell = ds.sel(latitude=latitude, longitude=longitude, method="nearest")
            if need_solar:
                ghi_parts.append(cell["GHI"].to_series())
                if "PS" in cell:
                    ps_parts.append(cell["PS"].to_series())
            if need_t:
                t_parts.append(_temperature_series(cell, legacy_temperature_var))
            for v in wind_vars:
                if v not in cell:
                    raise KeyError(
                        f"{p.name} has no {v!r} variable; this archive "
                        f"predates {canonical}'s wind-export convention. "
                        "Re-run the pipeline's transform+export phase for "
                        "this file to regenerate it with wind variables."
                    )
                wind_parts[v].append(cell[v].to_series())
            cell_lat = float(cell["latitude"])
            cell_lon = float(cell["longitude"])

    if unrepaired and need_solar:
        raise RuntimeError(
            f"{canonical} archive has {len(unrepaired)} unrepaired month(s) "
            f"under {out_dir}: {sorted(unrepaired)}. Their first hourly "
            "GHI/SNOWFALL stamp still holds a raw accumulated daily total "
            "(an implausible spike, not a real flux) rather than an hourly "
            "flux. Run weather.providers.era5_land.boundary_repair."
            "repair_boundaries() (or `python -m weather.providers."
            "era5_land.boundary_repair`) against this output directory "
            "before using it for point queries."
        )

    columns: dict[str, pd.Series] = {}
    if need_t:
        columns["T"] = pd.concat(t_parts).sort_index()
    if need_solar:
        ghi = pd.concat(ghi_parts).sort_index()
        columns["GHI"] = ghi
        if "DNI" in variables or "DHI" in variables:
            pressure = pd.concat(ps_parts).sort_index() if ps_parts else None
            dni_dhi = reconstruct_dni_dhi(
                ghi, cell_lat, cell_lon, method="dirint", pressure=pressure
            )
            if "DNI" in variables:
                columns["DNI"] = _strip_tz(dni_dhi["DNI"])
            if "DHI" in variables:
                columns["DHI"] = _strip_tz(dni_dhi["DHI"])
    for v in wind_vars:
        columns[v] = pd.concat(wind_parts[v]).sort_index()

    return _finalize(pd.DataFrame(columns), variables)


def _get_point_era5_land(
    latitude: float, longitude: float, year: int, data_dir: Path | str | None,
    variables: tuple[str, ...],
) -> pd.DataFrame:
    return _get_point_regular_grid(
        latitude, longitude, year, data_dir, variables,
        canonical="era5-land",
        filename_glob=f"ERA5_LAND_{year}_??_all_attrs.nc",
        legacy_pressure_var="sp",
        legacy_temperature_var="t2m",
    )


def _get_point_merra2(
    latitude: float, longitude: float, year: int, data_dir: Path | str | None,
    variables: tuple[str, ...],
) -> pd.DataFrame:
    return _get_point_regular_grid(
        latitude, longitude, year, data_dir, variables,
        canonical="merra-2",
        filename_glob=f"MERRA2_{year}_??_all_attrs.nc",
        legacy_pressure_var="PS",
        legacy_temperature_var="T2M",
    )


def _get_point_cosmo_rea6(
    latitude: float, longitude: float, year: int, data_dir: Path | str | None,
    variables: tuple[str, ...],
) -> pd.DataFrame:
    xr = _import_xarray()
    out_dir = _output_dir("cosmo-rea6", data_dir)

    annual_path = out_dir / f"COSMO_REA6_{year}_annual_all_attrs.nc"
    if annual_path.exists():
        datasets = [xr.open_dataset(str(annual_path))]
    else:
        paths = sorted(out_dir.glob(f"COSMO_REA6_{year}_??_all_attrs.nc"))
        if not paths:
            raise FileNotFoundError(
                f"No processed cosmo-rea6 file for {year} under {out_dir} "
                f"(looked for COSMO_REA6_{year}_annual_all_attrs.nc or "
                f"COSMO_REA6_{year}_??_all_attrs.nc). Run the pipeline for "
                f"{year} first (weather run --provider cosmo-rea6 --year "
                f"{year})."
            )
        # This is the flat, non-country-scoped fallback (no country's
        # bounding box covers this point -- see _resolve_country_dir) and
        # unlike a country-scoped archive it is not guaranteed to be a
        # complete year: only whichever months the pipeline has been run
        # for exist here. A caller asking for one year/location should
        # never silently get back a handful of months with a 200 -- fail
        # loudly instead, the same way an unrepaired ERA5-Land boundary
        # month does below.
        found_months = {p.name.removeprefix(f"COSMO_REA6_{year}_")[:2] for p in paths}
        missing_months = sorted(f"{m:02d}" for m in range(1, 13) if f"{m:02d}" not in found_months)
        if missing_months:
            raise RuntimeError(
                f"cosmo-rea6 archive for {year} under {out_dir} only has "
                f"month(s) {sorted(found_months)} processed; missing "
                f"{missing_months}. ({latitude}, {longitude}) falls outside "
                "every country-scoped archive (see COUNTRIES in "
                "weather.geo.countries), so it fell back to this flat, "
                "partial archive. Run the pipeline for the missing months, "
                "or query a location inside a country-scoped archive with "
                "full-year coverage."
            )
        # Open each monthly file independently rather than
        # xr.open_mfdataset(..., combine="by_coords"): a real COSMO
        # archive can straddle the lat/lon-retention fix (some months
        # regenerated with 2-D latitude/longitude coordinates, others
        # not — see NEXT MAJOR TASKS item 5/6 in CLAUDE.md).
        # combine="by_coords" raises "'latitude' not present in all
        # datasets" the instant one month has the coordinate and
        # another doesn't. The physical (y, x) grid is identical across
        # every COSMO export regardless of whether a given month's file
        # carries the coordinate labels, so the nearest cell is resolved
        # ONCE from whichever file has them and the same index is
        # selected from every file.
        datasets = [xr.open_dataset(str(p)) for p in paths]

    ref = next(
        (d for d in datasets if "latitude" in d.coords and "longitude" in d.coords),
        None,
    )
    if ref is None:
        for d in datasets:
            d.close()
        raise KeyError(
            f"No processed cosmo-rea6 file for {year} under {out_dir} has "
            "latitude/longitude coordinates; re-run the pipeline's "
            "transform+export phase to regenerate them."
        )
    iy, ix = find_nearest_cell(ref, latitude, longitude)
    cell_lat = float(ref["latitude"].isel(y=iy, x=ix))
    cell_lon = float(ref["longitude"].isel(y=iy, x=ix))

    need_solar = any(v in _SOLAR_DERIVED for v in variables)
    need_t = "T" in variables
    wind_vars = [v for v in variables if v in _WIND_VARS]

    cells = [d.isel(y=iy, x=ix) for d in datasets]
    columns: dict[str, pd.Series] = {}
    if need_t:
        columns["T"] = pd.concat(
            [_temperature_series(c, "T_2M") for c in cells]
        ).sort_index()
    ghi = None
    if need_solar:
        ghi = pd.concat([c["GHI"].to_series() for c in cells]).sort_index()
        columns["GHI"] = ghi
    for v in wind_vars:
        for c in cells:
            if v not in c:
                for d in datasets:
                    d.close()
                raise KeyError(
                    f"cosmo-rea6 archive for {year} has no {v!r} variable; "
                    "it predates cosmo-rea6's wind-export convention "
                    "(include_wind_components). Re-run the pipeline's "
                    "transform+export phase to regenerate it with wind "
                    "variables."
                )
        columns[v] = pd.concat([c[v].to_series() for c in cells]).sort_index()

    for d in datasets:
        d.close()

    # COSMO-REA6's own per-cell DNI (present when compute_dni_field=True at
    # pipeline time) is explicitly experimental/unreliable near the horizon
    # (see providers.cosmo_rea6.transform.compute_dni's docstring). Always
    # reconstruct DNI/DHI from GHI instead — the same DISC variant buem's
    # own pipeline historically trusted for this data source.
    if need_solar and ("DNI" in variables or "DHI" in variables):
        dni_dhi = reconstruct_dni_dhi(
            ghi, cell_lat, cell_lon,
            method="disc",
            zenith_kind="apparent",
            clip_to_extraterrestrial=True,
            clip_dhi_to_ghi=True,
        )
        if "DNI" in variables:
            columns["DNI"] = _strip_tz(dni_dhi["DNI"])
        if "DHI" in variables:
            columns["DHI"] = _strip_tz(dni_dhi["DHI"])

    return _finalize(pd.DataFrame(columns), variables)


_DISPATCH = {
    "era5-land": _get_point_era5_land,
    "merra-2": _get_point_merra2,
    "cosmo-rea6": _get_point_cosmo_rea6,
}


def get_point_weather(
    latitude: float,
    longitude: float,
    year: int,
    *,
    provider: str = "era5-land",
    data_dir: Path | str | None = None,
    variables: str | list[str] | tuple[str, ...] | None = None,
    use_case: str | None = None,
) -> pd.DataFrame:
    """Return an hourly DataFrame of the requested variables for one
    location/year.

    Extracts the nearest already-processed grid cell to *(latitude,
    longitude)* for *year*. Does **not** download or process data — call
    the relevant provider's pipeline first if nothing has been processed
    yet for that year.

    Parameters
    ----------
    latitude, longitude : float
        Target location in degrees (WGS84).
    year : int
        Calendar year to fetch.
    provider : str
        One of ``"era5-land"`` (default), ``"cosmo-rea6"``, ``"merra-2"``
        (aliases ``era5``, ``cosmo``, ``merra2`` also accepted).
    data_dir : Path or str, optional
        Directory containing the provider's already-exported ``.nc``
        files. If omitted, first tries a country-scoped archive whose
        bounding box contains *(latitude, longitude)* (the
        ``<provider>/<country>/output`` layout — see
        ``docs/COUNTRY_SCOPED_ARCHIVES.md``), then falls back to that
        provider's flat ``<work_dir>/output`` convention
        (``weather.common.env.data_root()/<provider>/output``) if no
        country-scoped archive covers this location/year.
    variables : str or list of str, optional
        Comma-separated string or list of canonical variable names (see
        ``weather.variables.VARIABLES``) to return, e.g. ``"T,GHI"`` or
        ``["WS_10M", "U_10M", "V_10M"]``. Exactly one of *variables*/
        *use_case* is required.
    use_case : str, optional
        Shorthand for a named variable set (see
        ``weather.variables.USE_CASES``), e.g. ``"solar"`` or ``"wind"``.
        Exactly one of *variables*/*use_case* is required -- no default,
        so a caller that forgets to say what it needs gets a clear
        error instead of a silently-substituted guess.

    Returns
    -------
    pandas.DataFrame
        Tz-naive hourly ``DatetimeIndex``, one column per resolved
        variable, in the order requested.

    Raises
    ------
    WeatherAPIError
        A ``ValueError`` subclass carrying a stable ``.code`` (see
        ``weather.errors``). Raised if *provider* is not recognized,
        both *variables* and *use_case* are given, or either names
        something unknown.
    FileNotFoundError
        If no processed archive exists for *(provider, year)* under
        *data_dir*.
    KeyError
        If a requested variable isn't present in this archive (e.g. wind
        requested against a pre-wind-export archive).
    ImportError
        If the ``pointquery`` (xarray/netcdf4) or ``solar`` (pvlib)
        extras are not installed.
    """
    canonical = resolve_provider(provider)

    resolved_variables = resolve_variables(variables=variables, use_case=use_case)

    if data_dir is None:
        data_dir = _resolve_country_dir(canonical, latitude, longitude, year)

    return _DISPATCH[canonical](latitude, longitude, year, data_dir, resolved_variables)


def resolve_provider(provider: str) -> str:
    """Validate and canonicalize a provider name or alias.

    Shared by ``get_point_weather()`` and the HTTP layer's
    ``/v1/weather/validate`` (structural validation only, no archive
    access).

    Raises
    ------
    WeatherAPIError
        If *provider* is not a recognized name or alias.
    """
    normalized = str(provider).strip().lower().replace("_", "-")
    canonical = _ALIASES.get(normalized)
    if canonical is None:
        available = ", ".join(sorted(set(_ALIASES.values())))
        raise WeatherAPIError(
            errors.UNKNOWN_PROVIDER,
            f"Unknown provider: {provider!r}. Available: {available}",
            details={"provider": provider},
        )
    return canonical
