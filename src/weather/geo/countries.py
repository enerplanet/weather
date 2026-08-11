"""European country bounding boxes.

Trimmed port of ``countries.py`` from the downstream
``THD-Spatial-AI/merra2-energy-pipeline`` consumer. That repo should only
do energy-potential analysis on data already cropped to its country of
interest — the bbox data and the cropping itself (:mod:`weather.geo.crop`)
now live here instead.

Two things were deliberately dropped from the upstream source:

* **Timezones** — not needed by anything downstream of a bbox crop.
* **The German/French/English multilingual alias table** (``deutschland``
  -> ``germany``, etc.) — dropped per product decision, not partially
  kept. ``uk``/``united_kingdom`` and ``czech_republic``/``czechia``
  remain because upstream defines them as two independent canonical
  entries (not aliases), and this module keeps the same distinction.
  """

from __future__ import annotations

from .bbox import BBox

# Values ported verbatim from upstream (lon_min, lat_min, lon_max, lat_max),
# reordered into this repo's own BBox(north, west, south, east) convention.
COUNTRIES: dict[str, BBox] = {
    "germany": BBox(north=54.983, west=5.988, south=47.302, east=15.016),
    "france": BBox(north=51.089, west=-5.142, south=42.333, east=8.231),
    "spain": BBox(north=43.791, west=-9.301, south=36.000, east=3.315),
    "italy": BBox(north=47.092, west=6.627, south=36.649, east=18.520),
    "uk": BBox(north=58.635, west=-8.182, south=49.959, east=1.769),
    "united_kingdom": BBox(north=58.635, west=-8.182, south=49.959, east=1.769),
    "poland": BBox(north=54.836, west=14.123, south=49.002, east=24.146),
    "netherlands": BBox(north=53.472, west=3.358, south=50.751, east=7.210),
    "belgium": BBox(north=51.505, west=2.546, south=49.497, east=6.404),
    "austria": BBox(north=49.021, west=9.531, south=46.372, east=17.161),
    "switzerland": BBox(north=47.808, west=5.956, south=45.818, east=10.492),
    "portugal": BBox(north=42.154, west=-9.500, south=36.960, east=-6.189),
    "sweden": BBox(north=69.060, west=11.109, south=55.337, east=24.167),
    "norway": BBox(north=71.185, west=4.650, south=57.960, east=31.078),
    "finland": BBox(north=70.092, west=20.649, south=59.808, east=31.587),
    "denmark": BBox(north=57.752, west=8.089, south=54.568, east=15.159),
    "ireland": BBox(north=55.380, west=-10.478, south=51.422, east=-5.998),
    "czech_republic": BBox(north=51.058, west=12.091, south=48.552, east=18.859),
    "czechia": BBox(north=51.058, west=12.091, south=48.552, east=18.859),
    "romania": BBox(north=48.265, west=20.262, south=43.619, east=29.691),
    "hungary": BBox(north=48.585, west=16.112, south=45.737, east=22.898),
    "greece": BBox(north=41.749, west=19.374, south=34.802, east=29.645),
    "croatia": BBox(north=46.555, west=13.490, south=42.393, east=19.427),
    "bulgaria": BBox(north=44.215, west=22.357, south=41.236, east=28.612),
    "slovakia": BBox(north=49.603, west=16.847, south=47.728, east=22.558),
    "slovenia": BBox(north=46.877, west=13.383, south=45.421, east=16.607),
    "luxembourg": BBox(north=50.183, west=5.734, south=49.448, east=6.531),
    "estonia": BBox(north=59.676, west=21.774, south=57.516, east=28.210),
    "latvia": BBox(north=58.082, west=20.971, south=55.675, east=28.241),
    "lithuania": BBox(north=56.450, west=20.932, south=53.901, east=26.836),
}


def normalize_country(country: str) -> str:
    """Normalize user-provided country strings to internal keys.

    Lowercases, strips whitespace, and replaces internal whitespace with
    underscores (e.g. ``"Czech Republic"`` -> ``"czech_republic"``). No
    alias resolution — see module docstring.
    """
    return "_".join(country.strip().lower().split())


def get_bbox(country: str) -> BBox:
    """Return the :class:`~weather.geo.bbox.BBox` for *country*.

    Raises
    ------
    ValueError
        If *country* is not a recognised key, listing available names.
    """
    key = normalize_country(country)
    if key not in COUNTRIES:
        available = ", ".join(list_countries())
        raise ValueError(f"Unknown country: '{country}'. Available: {available}")
    return COUNTRIES[key]


def list_countries() -> list[str]:
    """Return the sorted list of all recognised country keys."""
    return sorted(COUNTRIES.keys())
