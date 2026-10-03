"""Export processed COSMO-REA6 data to compressed NetCDF.

Writes the per-month :class:`xarray.Dataset` produced by
:func:`~weather.providers.cosmo_rea6.transform.build_month_dataset` to a
single NetCDF-4 file with zlib compression, following CF-1.8 conventions.
``pipeline.py`` always passes an explicit ``output_path``
(``COSMO_REA6_<year>_<month>_all_attrs.nc``, matching ERA5-Land/MERRA-2's
monthly convention); this module's own ``COSMO_REA6_<year>.nc`` default
below only applies to a direct, path-less call.

Typical usage::

    from weather.providers.cosmo_rea6.export import export_netcdf
    export_netcdf(ds, Path("/data/output/COSMO_REA6_2018_01_all_attrs.nc"))
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    import xarray  # noqa: F401  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Prevent HDF5 file-locking deadlocks on network/parallel file systems (GPFS).
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")


def _add_cf_grid_attrs(ds: xarray.Dataset) -> None:
    """Attach CF metadata to the 2-D lat/lon coordinates in place.

    Without ``standard_name``/``units`` on the coordinate variables, CDO
    (and other CF-aware tools) can't tell this is a curvilinear lat/lon
    grid and reports "Unsupported grid type: generic" -- exactly the
    failure that blocked ``weather.geo.crop.crop_to_country()`` against
    COSMO-REA6 output. Same category of gap as the fix already applied to
    ``merra2``/``era5_land``'s transform.py for their regular grids; this
    is COSMO's curvilinear equivalent.

    Deliberately does NOT set a ``coordinates`` attribute on each data
    variable by hand -- xarray's own ``to_netcdf()`` already detects
    ``latitude``/``longitude`` as non-dimension coordinates and populates
    each variable's ``encoding["coordinates"]`` automatically; setting
    the same thing in ``attrs`` too raises ``ValueError: 'coordinates'
    found in both attrs and encoding`` at write time (hit this for real
    on the first attempt). Native rotated-pole rlat/rlon axes and a
    formal CF ``grid_mapping`` variable are also deliberately not
    reconstructed here -- that's a bigger change than a bbox crop needs,
    and every rotation parameter (``GRIB_latitudeOfSouthernPoleInDegrees``
    etc.) already survives on each data variable's own GRIB_* attrs for
    anyone who later needs it.
    """
    if "latitude" in ds.coords:
        ds["latitude"].attrs.update({
            "standard_name": "latitude",
            "long_name": "latitude",
            "units": "degrees_north",
        })
    if "longitude" in ds.coords:
        ds["longitude"].attrs.update({
            "standard_name": "longitude",
            "long_name": "longitude",
            "units": "degrees_east",
        })


def _build_encoding(ds: xarray.Dataset, complevel: int = 1) -> dict:
    """Build per-variable NetCDF encoding with zlib compression.

    Parameters
    ----------
    ds : xarray.Dataset
        The dataset to encode.
    complevel : int
        zlib compression level (1=fastest, 9=smallest; 1 is recommended
        because levels 2–9 give diminishing returns for much higher CPU
        cost on large grids like COSMO-REA6 824×848).

    Returns
    -------
    dict
        Encoding dict suitable for :meth:`xarray.Dataset.to_netcdf`.
    """
    encoding = {}
    for var in ds.data_vars:
        encoding[var] = {
            "zlib": True,
            "complevel": complevel,
            # Use float32 for radiation/temperature/wind to halve file size
            # without meaningful precision loss (instruments are ~0.1 W/m²).
            "dtype": "float32",
        }
    return encoding


def export_netcdf(
    ds: xarray.Dataset,
    output_path: Path | None = None,
    *,
    complevel: int = 1,
    year: int | None = None,
) -> Path:
    """Write the processed dataset to a compressed NetCDF-4 file.

    Parameters
    ----------
    ds : xarray.Dataset
        Processed annual weather dataset.
    output_path : Path, optional
        Full path for the output file.  If omitted, defaults to
        ``<work_dir>/output/COSMO_REA6_<year>.nc``.
    complevel : int
        zlib compression level (default 1 — fastest; levels 2–9 give
        minimal size reduction for much higher CPU cost on large grids).
    year : int, optional
        Year label for the default filename.

    Returns
    -------
    Path
        Path to the written NetCDF file.
    """
    if output_path is None:
        from .config import get_config
        cfg = get_config()
        yr = year or cfg["year"]
        fname = f"COSMO_REA6_{yr}.nc"
        output_path = cfg["output_dir"] / fname

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Materialise dask arrays into in-memory arrays BEFORE writing.
    # Compute one variable at a time to limit peak memory usage —
    # loading all variables simultaneously would require ~12 GiB,
    # but sequential computation peaks at ~4 GiB per variable.
    t0 = time.perf_counter()
    logger.info("Computing dask arrays into memory (variable-by-variable)...")
    for var_name in list(ds.data_vars):
        if hasattr(ds[var_name].data, "dask"):
            logger.info("  Computing %s ...", var_name)
            ds[var_name] = ds[var_name].compute()
    logger.info("  All variables computed in %.1f s", time.perf_counter() - t0)

    _add_cf_grid_attrs(ds)
    encoding = _build_encoding(ds, complevel=complevel)
    logger.info("Writing NetCDF to %s (complevel=%d)", output_path, complevel)

    # Atomic write (temp -> rename): a direct ds.to_netcdf(output_path, ...)
    # writes variable-by-variable into the target file in place, so any
    # interruption mid-write (Ctrl-C, OOM kill, crash) leaves a truncated
    # file sitting at the FINAL path -- which run_pipeline's naive
    # `resume and out_path.exists()` check then treats as already done,
    # silently baking a corrupt/incomplete month into the archive forever
    # (found via a real interrupted run during this session: a killed
    # process left a file with only 4 of 13 expected variables, and a
    # subsequent --resume run skipped reprocessing it). Writing to a
    # sibling .tmp path and renaming only after a full, successful write
    # matches every other exporter in this repo (ERA5-Land/MERRA-2's
    # export.py, boundary_repair.py's _write()).
    t1 = time.perf_counter()
    tmp_path = output_path.with_suffix(".nc.tmp")
    ds.to_netcdf(
        tmp_path,
        encoding=encoding,
        format="NETCDF4",
        engine="netcdf4",
    )
    tmp_path.replace(output_path)
    logger.info("  NetCDF write done in %.1f s", time.perf_counter() - t1)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("NetCDF written: %s (%.1f MB)", output_path.name, size_mb)
    return output_path


def export_single_point_csv(
    ds: xarray.Dataset,
    rlat_idx: int,
    rlon_idx: int,
    output_path: Path,
) -> Path:
    """Extract a single grid-cell time series and write to CSV.

    Useful for extracting weather data for a specific building location
    in the format expected by :class:`~weather.from_csv.CsvWeatherData`.

    Parameters
    ----------
    ds : xarray.Dataset
        Processed annual dataset (must contain ``T``, ``GHI``, ``DHI``).
    rlat_idx, rlon_idx : int
        Grid indices (0-based) in the rotated-pole grid.
    output_path : Path
        Output CSV file path.

    Returns
    -------
    Path
        Path to the written CSV.

    Notes
    -----
    The output CSV contains columns ``T``, ``GHI``, ``DHI`` matching the
    format expected by :class:`~weather.from_csv.CsvWeatherData`.
    DNI must be reconstructed by the thermal model using pvlib DISC from GHI.
    """
    logger.info(
        "Extracting single point (rlat=%d, rlon=%d) to %s",
        rlat_idx, rlon_idx, output_path,
    )

    point = ds.isel(y=rlat_idx, x=rlon_idx)

    df = pd.DataFrame(
        {
            "T": point["T"].values,
            "GHI": point["GHI"].values,
            "DHI": point["DHI"].values,
        },
        index=pd.to_datetime(point["time"].values, utc=True),
    )
    df.index.name = "datetime"

    # Add WS_10M/ALBEDO if present
    if "WS_10M" in point:
        df["WS_10M"] = point["WS_10M"].values
    if "ALBEDO" in point:
        df["ALBEDO"] = point["ALBEDO"].values

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path)
    logger.info(
        "Single-point CSV written: %s (%d rows)",
        output_path.name,
        len(df),
    )
    return output_path
