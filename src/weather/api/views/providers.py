"""GET /v1/weather/providers -- which years and scenarios each provider has processed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from flask import jsonify
from flask.views import MethodView

from ... import errors
from ...errors import error_body
from ...point_query import SCENARIOS, _import_xarray, _monthly_glob

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


def _available_years(provider: str) -> list[int]:
    """Years with a processed archive for *provider*, inferred from
    filenames already on disk.

    Deliberately returns only the derived year list, not a directory
    listing -- keeps /v1/weather/health from becoming a bulk-discovery
    endpoint.
    """
    output_dir = _output_dir(provider)
    if output_dir is None:
        return []

    years: set[int] = set()
    for path in output_dir.glob("*.nc"):
        # e.g. MERRA2_2018_01_all_attrs.nc / COSMO_REA6_2018_01_all_attrs.nc
        for part in path.stem.split("_"):
            if part.isdigit() and len(part) == 4:
                years.add(int(part))
                break
    return sorted(years)


def _scenario_coverage(provider: str) -> tuple[list[str], list[int]]:
    """Scenarios whose 12 monthly percentile files are all present, and
    the source years those files draw on (their ``source_year`` grids).
    """
    output_dir = _output_dir(provider)
    if output_dir is None:
        return [], []
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


class ProvidersView(MethodView):
    def get(self) -> Any:
        from weather.registry import list_providers

        providers: dict[str, dict[str, Any]] = {}
        for name in list_providers():
            try:
                scenarios, source_years = _scenario_coverage(name)
                providers[name] = {
                    "years": _available_years(name),
                    "scenarios": scenarios,
                    "scenario_source_years": source_years,
                }
            except OSError as exc:
                providers[name] = {
                    "error": error_body(errors.PROVIDER_LISTING_FAILED, str(exc))
                }
        return jsonify(providers=providers)
