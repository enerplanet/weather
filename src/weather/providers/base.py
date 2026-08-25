"""Provider contract for weather data sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

#: Issues returned by validate_environment() prefixed with this marker are
#: advisory (degraded but still fully functional -- e.g. a missing
#: optional binary with a working, slower fallback), not critical
#: (actually broken). cli.py's _cmd_validate fails the `weather validate`
#: exit code only on unprefixed issues. A plain string-prefix convention
#: rather than a richer return type, so it doesn't need a
#: WeatherProvider Protocol change -- providers with no advisory-class
#: issues (currently era5-land, merra-2) are unaffected either way.
ADVISORY_PREFIX = "[advisory] "


class WeatherProvider(Protocol):
    """Minimal provider interface used by the CLI registry."""

    name: str

    def get_config_summary(self) -> dict[str, Any]:
        """Return provider-specific resolved configuration."""

    def validate_environment(self) -> list[str]:
        """Return environment issues (empty list means OK). See
        ADVISORY_PREFIX above for the advisory/critical distinction."""

    def run_pipeline(
        self,
        year: int | None = None,
        attributes: list[str] | None = None,
        *,
        work_dir: Path | None = None,
        output_path: Path | None = None,
        include_wind_components: bool = True,
        complevel: int = 1,
        skip_download: bool = False,
        skip_decompress: bool = False,
        cleanup: bool = False,
    ) -> Path:
        """Execute provider pipeline and return output artifact path."""
