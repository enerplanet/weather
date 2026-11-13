"""GET /v1/weather/providers -- which years and scenarios each provider has
processed, overall and per country archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from flask import jsonify
from flask.views import MethodView

from ... import errors
from ...common.env import data_root
from ...errors import error_body
from ...point_query import _OUTPUT_SUBDIR, SCENARIOS, _import_xarray, _monthly_glob

_PROVIDER_OUTPUT_DIR_GETTERS = {
    "merra-2": "merra2_output_dir",
    "cosmo-rea6": "cosmo_output_dir",
    "era5-land": "era5_output_dir",
}


def _output_dir(provider: str) -> Path | None:
    """The flat processed-output directory for *provider*, or ``None``."""
    from weather.settings import EnvSettings

    attr = _PROVIDER_OUTPUT_DIR_GETTERS.get(provider)
    if attr is None:
        return None
    output_dir: Path = getattr(EnvSettings, attr)()
    return output_dir if output_dir.is_dir() else None


def _archive_dirs(provider: str) -> dict[str, Path]:
    """Processed-output directories for *provider*: the flat directory
    under key ``""`` and each ``<provider>/<country>/output`` directory
    under its country name, the same layout point queries read.
    """
    from weather.geo.countries import COUNTRIES

    dirs: dict[str, Path] = {}
    flat = _output_dir(provider)
    if flat is not None:
        dirs[""] = flat
    root = data_root() / _OUTPUT_SUBDIR[provider]
    for country in COUNTRIES:
        country_dir = root / country / "output"
        if country_dir.is_dir():
            dirs[country] = country_dir
    return dirs


def _years_in(output_dir: Path) -> list[int]:
    """Years with a processed archive in *output_dir*, inferred from
    filenames already on disk.

    Deliberately returns only the derived year list, not a directory
    listing -- keeps /v1/weather/health from becoming a bulk-discovery
    endpoint.
    """
    years: set[int] = set()
    for path in output_dir.glob("*.nc"):
        # e.g. MERRA2_2018_01_all_attrs.nc / COSMO_REA6_2018_01_all_attrs.nc
        for part in path.stem.split("_"):
            if part.isdigit() and len(part) == 4:
                years.add(int(part))
                break
    return sorted(years)


def _scenario_coverage(provider: str, output_dir: Path) -> tuple[list[str], list[int]]:
    """Scenarios whose 12 monthly percentile files are all present in
    *output_dir*, and the source years those files draw on (their
    ``source_year`` grids).
    """
    xr = _import_xarray()
    scenarios: list[str] = []
    source_years: set[int] = set()
    for scenario in SCENARIOS:
        paths = sorted((output_dir / "percentile").glob(_monthly_glob(provider, scenario)))
        if len(paths) != 12:
            continue
        scenarios.append(scenario)
        # Re-reads 12 small grids per scenario per request; cache on mtime if slow.
        for path in paths:
            with xr.open_dataset(path) as ds:
                source_years.update(int(y) for y in np.unique(ds["source_year"].values))
    source_years.discard(-1)
    return scenarios, sorted(source_years)


def _provider_entry(provider: str) -> dict[str, Any]:
    """Years and scenarios across every archive directory of *provider*,
    plus the same per country."""
    years: set[int] = set()
    scenarios: set[str] = set()
    source_years: set[int] = set()
    countries: dict[str, dict[str, list[Any]]] = {}
    for country, output_dir in _archive_dirs(provider).items():
        dir_years = _years_in(output_dir)
        dir_scenarios, dir_source_years = _scenario_coverage(provider, output_dir)
        years.update(dir_years)
        scenarios.update(dir_scenarios)
        source_years.update(dir_source_years)
        if country:
            countries[country] = {"years": dir_years, "scenarios": dir_scenarios}
    return {
        "years": sorted(years),
        "scenarios": [s for s in SCENARIOS if s in scenarios],
        "scenario_source_years": sorted(source_years),
        "countries": countries,
    }


class ProvidersView(MethodView):
    def get(self) -> Any:
        from weather.registry import list_providers

        providers: dict[str, dict[str, Any]] = {}
        for name in list_providers():
            try:
                providers[name] = _provider_entry(name)
            except OSError as exc:
                providers[name] = {
                    "error": error_body(errors.PROVIDER_LISTING_FAILED, str(exc))
                }
        return jsonify(providers=providers)
