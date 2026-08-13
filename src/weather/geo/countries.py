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
