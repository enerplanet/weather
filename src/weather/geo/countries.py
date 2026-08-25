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

Entries live in ``countries.json`` next to this file, not inline, so a
new region — a country or a smaller custom area (a state, a city) — can
be added by editing data instead of Python. Any ``{name: {north, west,
south, east}}`` entry works the same way regardless of what the name
refers to.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bbox import BBox

_COUNTRIES_FILE = Path(__file__).with_name("countries.json")


def _load_countries() -> dict[str, BBox]:
    with _COUNTRIES_FILE.open() as f:
        raw: dict[str, dict[str, float]] = json.load(f)
    return {name: BBox(**coords) for name, coords in raw.items()}


COUNTRIES: dict[str, BBox] = _load_countries()


# ISO 3166-1 alpha-2 codes, used as the region tag in fixture/output
# filenames for a country-scoped ``weather fetch`` (e.g.
# ``MERRA2_NL_2023_01_all_attrs.nc``). Real ISO codes, not invented
# shorthand -- immediately recognisable, no bikeshedding. One deliberate
# note: the UK's real ISO code is "GB" (Great Britain), not "UK" --
# used here despite "UK" being the more colloquial spelling, matching
# ISO 3166-1 rather than common usage. Both `uk`/`united_kingdom` keys
# map to the same "GB" code, consistent with COUNTRIES treating them as
# two independent entries for the same country.
COUNTRY_CODES: dict[str, str] = {
    "austria": "AT",
    "belgium": "BE",
    "bulgaria": "BG",
    "croatia": "HR",
    "czech_republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "hungary": "HU",
    "ireland": "IE",
    "italy": "IT",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "slovakia": "SK",
    "slovenia": "SI",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "uk": "GB",
    "united_kingdom": "GB",
}


def get_country_code(country: str) -> str:
    """Return the ISO 3166-1 alpha-2 code for *country* (see
    :data:`COUNTRY_CODES`).

    Raises
    ------
    ValueError
        If *country* is not a recognised key (same message/listing as
        :func:`get_bbox`).
    """
    key = normalize_country(country)
    if key not in COUNTRY_CODES:
        available = ", ".join(list_countries())
        raise ValueError(f"Unknown country: '{country}'. Available: {available}")
    return COUNTRY_CODES[key]


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
