"""NetCDF bounding-box cropping via CDO.

Actually crops output NetCDFs to a country's extent (not just a lookup),
so that downstream consumers — currently
``THD-Spatial-AI/merra2-energy-pipeline`` — receive already-cropped data
and don't need their own geo-cropping logic.

Scope
-----
Works for all three providers' current exports (COSMO-REA6, ERA5-Land,
MERRA-2), each of which carries real ``latitude``/``longitude``
coordinates with the CF ``standard_name``/``units`` attributes CDO
requires to recognise them (COSMO: 2-D auxiliary coordinates on its
native rotated-pole grid, classified by CDO as ``curvilinear``;
ERA5-Land/MERRA-2: 1-D coordinates, classified as ``lonlat``) — see
``common/cf_conventions.attach_cf_latlon_attrs``, called from each
provider's ``transform.py``.

This was a real, confirmed gap until fixed: without those two
attributes (present until then only as bare ``_FillValue``-only
variables — an artefact of each provider's ``assign_coords`` call
building a fresh coordinate with no attributes), CDO logs
``Coordinates variable latitude can't be assigned!``, falls back to
``gridtype = generic`` with no lon/lat semantics at all, and
``sellonlatbox`` aborts outright (``Unsupported grid type: generic`` /
``No processable variable found!``) — verified against real exported
files for all three providers (via a real ``cdo sellonlatbox`` run,
not assumed). Archives exported before this fix landed do not have it
retroactively -- new exports get it automatically, but an existing
file will still hit the same abort until either re-exported or patched
in place (a cheap, metadata-only netCDF4 attribute fix -- no data
columns need touching or recomputing; not yet applied to any real
archive at the time of writing, only verified against small copies).

Not wired into any provider's ``pipeline.py``. This is a standalone
post-processing step, run against an already-exported output file (see
the ``weather geo crop`` CLI subcommand).

Typical usage::

    from weather.geo.crop import crop_to_country

    crop_to_country(
        Path("ERA5_Land_2020_06.nc"),
        Path("ERA5_Land_2020_06_netherlands.nc"),
        "netherlands",
    )
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .bbox import BBox
from .countries import get_bbox


def _cdo_available() -> bool:
    return shutil.which("cdo") is not None


def crop_netcdf(
    input_path: Path,
    output_path: Path,
    bbox: BBox,
    *,
    logger: logging.Logger | None = None,
) -> Path:
    """Crop *input_path* to *bbox* via ``cdo sellonlatbox``.

    Parameters
    ----------
    input_path : Path
        Source NetCDF with a regular (or CDO-recognised curvilinear)
        lat/lon grid.
    output_path : Path
        Destination path for the cropped NetCDF.
    bbox : BBox
        Crop extent.
    logger : logging.Logger, optional
        Logger for progress messages; defaults to this module's logger.

    Returns
    -------
    Path
        *output_path*, once the crop has completed successfully.

    Raises
    ------
    RuntimeError
        If the ``cdo`` binary is not on ``PATH`` (see
        ``infrastructure/env/weather_env.yml``).
    FileNotFoundError
        If *input_path* does not exist.
    subprocess.CalledProcessError
        If ``cdo`` itself fails (e.g. no lat/lon grid to crop against).
    """
    log = logger or logging.getLogger(__name__)
    if not input_path.exists():
        raise FileNotFoundError(f"Input NetCDF not found: {input_path}")
    if not _cdo_available():
        raise RuntimeError(
            "cdo is not installed or not on PATH. Install it via "
            "'conda install -c conda-forge cdo' — see "
            "infrastructure/env/weather_env.yml."
        )

    west, east, south, north = bbox.to_cdo_lonlatbox()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".part",
    )
    os.close(tmp_fd)

    args = [
        "cdo",
        "-O",  # tmp_path exists (mkstemp reserved it) -- force overwrite
        "-L",
        f"sellonlatbox,{west},{east},{south},{north}",
        str(input_path),
        tmp_path,
    ]
    log.info(
        "Cropping %s to [W=%s,E=%s,S=%s,N=%s]",
        input_path.name, west, east, south, north,
    )
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(
                "cdo failed (exit %d): %s",
                result.returncode, result.stderr.strip(),
            )
            raise subprocess.CalledProcessError(
                result.returncode, args,
                output=result.stdout, stderr=result.stderr,
            )
        Path(tmp_path).replace(output_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    log.info("Cropped NetCDF written: %s", output_path.name)
    return output_path


def crop_to_country(
    input_path: Path,
    output_path: Path,
    country: str,
    *,
    logger: logging.Logger | None = None,
) -> Path:
    """Crop *input_path* to *country*'s bbox (see :mod:`.countries`)."""
    return crop_netcdf(
        input_path, output_path, get_bbox(country), logger=logger
    )
