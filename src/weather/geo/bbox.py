"""Canonical bounding-box type and cross-tool convention converters.

This repo already has a bbox convention — ``ERA5_AREA`` / ``MERRA2_AREA``
both use ``[North, West, South, East]`` degrees (the CDS/OPeNDAP request
order; see :mod:`weather.providers.era5_land.config` and
``EnvSettings.merra2_area``). :class:`BBox` adopts that same field order
as its canonical representation, so :mod:`weather.geo.countries` values
plug directly into that existing pattern. Other tools use different axis
orders — :meth:`BBox.to_cdo_lonlatbox` converts explicitly rather than
leaving that translation to be re-derived (and likely gotten backwards)
at each call site.
"""

from __future__ import annotations

from typing import NamedTuple


class BBox(NamedTuple):
    """Geographic bounding box in degrees, WGS84.

    Field order matches this repo's existing ``[N, W, S, E]`` convention
    (``ERA5_AREA``, ``MERRA2_AREA``) — NOT the ``(lon_min, lat_min,
    lon_max, lat_max)`` order used by the upstream country-bbox source
    this module was ported from, and NOT CDO's ``sellonlatbox`` order
    either (see :meth:`to_cdo_lonlatbox`).
    """

    north: float
    west: float
    south: float
    east: float

    def to_area_list(self) -> list[float]:
        """Return ``[North, West, South, East]``.

        Drop-in value for ``ERA5_AREA`` / ``MERRA2_AREA``-style env
        config (both already expect this exact order).
        """
        return [self.north, self.west, self.south, self.east]

    def to_cdo_lonlatbox(self) -> tuple[float, float, float, float]:
        """Return ``(West, East, South, North)``.

        This is CDO's ``sellonlatbox,lon1,lon2,lat1,lat2`` argument
        order — a different axis order than this class's own ``[N, W,
        S, E]`` convention. Used by :func:`weather.geo.crop.crop_netcdf`.
        """
        return (self.west, self.east, self.south, self.north)

    @classmethod
    def parse(cls, raw: str) -> BBox:
        """Parse a ``"N,W,S,E"`` string into a :class:`BBox`.

        Same format/order as the ``ERA5_AREA``/``MERRA2_AREA`` env vars
        (see ``EnvSettings.era5_area``/``merra2_area`` and
        ``providers.era5_land.config._area_from_env``) — this is the one
        place that parsing logic lives; those three call sites duplicated
        it independently before this method existed.

        Raises
        ------
        ValueError
            If *raw* is not exactly 4 comma-separated numeric values.
        """
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError(
                f"bbox must be 'N,W,S,E' (4 numbers), got: {raw!r}"
            )
        try:
            north, west, south, east = (float(p) for p in parts)
        except ValueError as exc:
            raise ValueError(
                f"bbox values must be numeric, got: {raw!r}"
            ) from exc
        return cls(north=north, west=west, south=south, east=east)
