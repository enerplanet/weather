"""Country/bbox cropping for COSMO-REA6, applied right after decompress.

Pipeline position (COSMO-only — the other two providers crop server-side
before any local file exists, see ``unified_cli.py``)::

    download -> decompress -> crop (opt-in, --country/--bbox only) -> transform -> export

DWD has no server-side area-subsetting endpoint, so COSMO-REA6 always
downloads the whole fixed rotated-pole domain. This module lets a
country/bbox-scoped ``weather fetch`` skip the wasted compute of running
every derived-field formula (GHI, DHI, DNI, solar position, ...) over the
~99%+ of the domain outside the requested area — real end-to-end
measurement (Netherlands, 2018-01): the requested bbox covers only about
0.4% of the domain's cell count (56x50 of 824x848 cells).

Two-step design, deliberately separated:

1. :func:`compute_crop_index` — decode the target bbox into a ``(y, x)``
   index window, from ONE already-opened dataset's real cfgrib-decoded
   WGS84 ``latitude``/``longitude`` coordinates.
2. :func:`crop_datasets` — apply that index window to every attribute's
   dataset via ``.isel()`` (a cheap lazy view, not a copy).

The COSMO-REA6 domain is a fixed grid across the entire archive, so
:func:`compute_crop_index` only needs to run ONCE per bbox (e.g. from
whichever attribute :func:`~weather.providers.cosmo_rea6.transform
.build_month_dataset` opens first) — the same index window is valid for
every other attribute and every other month.

Rotated-pole geometry note
---------------------------
The returned index window is an axis-aligned RECTANGLE in ``(y, x)``
index space, guaranteed to CONTAIN every cell whose real WGS84 lat/lon
falls inside the requested bbox. Because COSMO-REA6's native grid is
rotated relative to true lat/lon, that index rectangle's own lat/lon
*extent* will be somewhat LARGER than the requested bbox (an
axis-aligned index box over a rotated grid cannot align exactly with an
axis-aligned WGS84 box — the corners "bulge" outward). This is expected,
not a bug: real end-to-end measurement (Netherlands) saw the cropped
lat/lon extent exceed the requested bbox by up to ~0.5 degrees at the
edges. A country crop always keeps at least the requested area, never
less, with some extra surrounding margin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import xarray

    from ...geo.bbox import BBox

logger = logging.getLogger(__name__)


def compute_crop_index(
    bbox: BBox, reference_ds: xarray.Dataset,
) -> tuple[slice, slice]:
    """Compute the ``(y, x)`` index window covering *bbox*.

    Parameters
    ----------
    bbox : BBox
        Target bounding box, WGS84 degrees.
    reference_ds : xarray.Dataset
        Any already-opened COSMO-REA6 dataset carrying real cfgrib-decoded
        2-D ``latitude``/``longitude`` coordinates (any attribute — the
        physical grid is identical across all of them).

    Returns
    -------
    tuple[slice, slice]
        ``(y_slice, x_slice)``, usable directly with ``.isel(y=..., x=...)``
        on any dataset sharing the same grid (see :func:`crop_datasets`).

    Raises
    ------
    ValueError
        If *reference_ds* has no ``latitude``/``longitude`` coordinates,
        or if no grid cell falls inside *bbox* (e.g. bbox outside
        COSMO-REA6's real domain).
    """
    if (
        "latitude" not in reference_ds.coords
        or "longitude" not in reference_ds.coords
    ):
        raise ValueError(
            "Dataset has no latitude/longitude coordinates -- cannot "
            "compute a crop index. cfgrib decodes these automatically "
            "from a real COSMO-REA6 GRIB (see transform.py's "
            "find_nearest_cell for the same requirement)."
        )
    lat_2d = reference_ds["latitude"].values
    lon_2d = reference_ds["longitude"].values
    mask = (
        (lat_2d >= bbox.south) & (lat_2d <= bbox.north)
        & (lon_2d >= bbox.west) & (lon_2d <= bbox.east)
    )
    if not mask.any():
        raise ValueError(
            f"No COSMO-REA6 grid cells fall inside bbox {bbox!r} -- "
            "outside the domain, or too small for this ~6 km resolution."
        )
    y_idx, x_idx = np.where(mask)
    y_slice = slice(int(y_idx.min()), int(y_idx.max()) + 1)
    x_slice = slice(int(x_idx.min()), int(x_idx.max()) + 1)
    logger.info(
        "Crop index for bbox %r: y=%s (%d cells) x=%s (%d cells), "
        "%.2f%% of full domain",
        bbox, y_slice, y_slice.stop - y_slice.start,
        x_slice, x_slice.stop - x_slice.start,
        100 * (y_slice.stop - y_slice.start) * (x_slice.stop - x_slice.start)
        / (reference_ds.sizes["y"] * reference_ds.sizes["x"]),
    )
    return y_slice, x_slice


def crop_datasets(
    datasets: dict[str, xarray.Dataset], y_slice: slice, x_slice: slice,
) -> dict[str, xarray.Dataset]:
    """Apply a precomputed ``(y, x)`` index window to every dataset.

    ``.isel()`` returns a lazy view (dask-backed data variables are not
    read/copied until computed), so this is cheap regardless of how many
    attributes are in *datasets*.

    Parameters
    ----------
    datasets : dict[str, xarray.Dataset]
        Attribute name -> opened dataset (e.g.
        :func:`~weather.providers.cosmo_rea6.transform.build_month_dataset`'s
        ``datasets`` dict).
    y_slice, x_slice : slice
        From :func:`compute_crop_index`.

    Returns
    -------
    dict[str, xarray.Dataset]
        New dict, same keys, each value cropped.
    """
    return {
        attr: ds.isel(y=y_slice, x=x_slice) for attr, ds in datasets.items()
    }
