"""Shared CF-Conventions metadata helpers.

Single-sourced across all three providers, since the same gap was found
independently in all three: each provider's ``transform.py`` re-attaches
``latitude``/``longitude`` as coordinates via ``xr.Dataset.assign_coords``
(either directly building the 2-D COSMO auxiliary coordinates, or, for
ERA5-Land/MERRA-2, restashing 1-D values after renaming their original
dims to ``y``/``x`` for cross-provider parity) -- ``assign_coords`` builds
a fresh coordinate variable with NO attributes, discarding whatever
``standard_name``/``units`` the source data may have carried.

This is invisible to xarray/``weather.point_query`` (both match by
variable name, not CF role), but breaks any stricter CF-aware external
tool -- confirmed via a real ``cdo sellonlatbox`` run against already-
exported COSMO and MERRA-2 files: with only ``_FillValue`` set on
``latitude``/``longitude``, cdo logged ``Coordinates variable latitude
can't be assigned!``, fell back to ``gridtype = generic`` (no lon/lat
semantics at all), and ``sellonlatbox`` aborted outright (``Unsupported
grid type: generic`` / ``No processable variable found!``). Each
provider's data variables already correctly declare their
``coordinates`` attribute (e.g. ``"latitude longitude"``) -- only the
coordinate variables' own identifying attributes were missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray


def attach_cf_latlon_attrs(ds: xarray.Dataset) -> None:
    """Attach ``standard_name``/``units`` to *ds*'s ``latitude``/
    ``longitude`` coordinates, in place.

    Works identically for 1-D (ERA5-Land/MERRA-2, dims ``(y,)``/``(x,)``)
    and 2-D (COSMO-REA6, dims ``(y, x)``) coordinates -- attribute
    assignment doesn't depend on dimensionality, only the variable
    itself, so one implementation covers all three providers.

    Parameters
    ----------
    ds : xarray.Dataset
        Must already have ``latitude``/``longitude`` coordinates
        assigned (e.g. via ``assign_coords``) -- this only attaches
        metadata, it does not compute or assign the coordinates
        themselves.
    """
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
