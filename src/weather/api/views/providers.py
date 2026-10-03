"""GET /v1/weather/providers -- which years each provider has processed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import jsonify
from flask.views import MethodView

from ... import errors
from ...errors import error_body

_PROVIDER_OUTPUT_DIR_GETTERS = {
    "merra-2": "merra2_output_dir",
    "cosmo-rea6": "cosmo_output_dir",
    "era5-land": "era5_output_dir",
}


def _available_years(provider: str) -> list[int]:
    """Years with a processed archive for *provider*, inferred from
    filenames already on disk.

    Deliberately returns only the derived year list, not a directory
    listing -- keeps /v1/weather/health from becoming a bulk-discovery
    endpoint.
    """
    from weather.settings import EnvSettings

    attr = _PROVIDER_OUTPUT_DIR_GETTERS.get(provider)
    if attr is None:
        return []
    output_dir: Path = getattr(EnvSettings, attr)()
    if not output_dir.is_dir():
        return []

    years: set[int] = set()
    for path in output_dir.glob("*.nc"):
        # e.g. MERRA2_2018_01_all_attrs.nc / COSMO_REA6_2018_01_all_attrs.nc
        for part in path.stem.split("_"):
            if part.isdigit() and len(part) == 4:
                years.add(int(part))
                break
    return sorted(years)


class ProvidersView(MethodView):
    def get(self) -> Any:
        from weather.registry import list_providers

        providers: dict[str, dict[str, Any]] = {}
        for name in list_providers():
            try:
                providers[name] = {"years": _available_years(name)}
            except OSError as exc:
                providers[name] = {
                    "error": error_body(errors.PROVIDER_LISTING_FAILED, str(exc))
                }
        return jsonify(providers=providers)
