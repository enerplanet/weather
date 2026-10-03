"""ERA5-Land pipeline configuration.

All settings resolve through :class:`~weather.settings.EnvSettings`, with
a few read directly from the environment here so they can be tuned via
``.env`` without editing ``settings.py``.

Typical usage::

    from weather.providers.era5_land.config import get_config
    cfg = get_config()
    print(cfg["work_dir"], cfg["area"])
"""

from __future__ import annotations

import os
from typing import Any

from ...settings import EnvSettings
from .downloaded_attributes import ATTRIBUTES


def _area_from_env() -> list[float]:
    """Parse ``ERA5_AREA`` = "N,W,S,E" into a list.

    CDS expects ``area = [North, West, South, East]`` in degrees. Falls
    back to ``WEATHER_REGION``'s bbox (see
    :meth:`EnvSettings.region_bbox`) when unset; with neither set,
    raises rather than silently defaulting to a global download (which
    is both far larger and hits eccodes memory-allocation failures on
    large global fields). See docs/COUNTRY_SCOPED_ARCHIVES.md for
    computing the box for a country set, or scoping to more than one
    country -- WEATHER_REGION only covers a single country.
    """
    raw = os.getenv("ERA5_AREA", "").strip()
    if not raw:
        region_bbox = EnvSettings.region_bbox()
        if region_bbox is not None:
            return region_bbox.to_area_list()
        raise ValueError(
            "ERA5_AREA is required (format 'N,W,S,E'), or set "
            "WEATHER_REGION to a single known country -- refusing to "
            "default to a global download."
        )
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(
            f"ERA5_AREA must be 'N,W,S,E' (4 numbers), got: {raw!r}"
        )
    try:
        return [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(
            f"ERA5_AREA values must be numeric, got: {raw!r}"
        ) from exc


def _region_tag_from_env() -> str | None:
    """``ERA5_REGION_TAG``, if set -- internal plumbing set by
    ``unified_cli.py`` alongside ``ERA5_AREA`` for a ``weather fetch
    --country``/``--bbox`` run, not a user-facing knob (not in
    ``.env.example``). ``None`` for every other caller, including the
    documented ``export ERA5_AREA=...`` bulk-run workflow -- see
    ``downloader.py``'s ``local_path``/``content_key``.
    """
    raw = os.getenv("ERA5_REGION_TAG", "").strip()
    return raw or None


def get_config() -> dict[str, Any]:
    """Return the fully-resolved ERA5-Land configuration."""
    return {
        # ---- paths ----
        "work_dir": EnvSettings.era5_work_dir(),
        "download_dir": EnvSettings.era5_download_dir(),
        "processed_dir": EnvSettings.era5_processed_dir(),
        "output_dir": EnvSettings.era5_output_dir(),

        # ---- what / when ----
        "year": EnvSettings.era5_year(),
        "attributes": list(ATTRIBUTES.keys()),

        # ---- compute ----
        "ncores": EnvSettings.era5_ncores(),
        "threads_per_job": EnvSettings.era5_threads_per_job(),
        "conda_env": EnvSettings.era5_conda_env(),
        "cleanup": EnvSettings.era5_cleanup(),

        # ---- CDS request ----
        "dataset": EnvSettings.era5_cds_dataset(),
        "data_format": EnvSettings.era5_data_format(),
        "download_format": EnvSettings.era5_download_format(),
        "cds_url": EnvSettings.era5_cds_url(),
        "cds_key": EnvSettings.era5_cds_key(),

        # Geographic crop [N, W, S, E]; required, see _area_from_env.
        "area": _area_from_env(),
        # Region tag for the downloaded raw file's name (e.g. "NL"),
        # None for the default/untagged case -- see local_path().
        "region_tag": _region_tag_from_env(),

        # CDS runs ~ONE job per account at a time; more just queues.
        "cds_max_concurrent": int(
            os.getenv("ERA5_CDS_MAX_CONCURRENT", "1")
        ),

        # Retry a transient CDS failure rather than losing a month during
        # a multi-week unattended run.
        "cds_max_retries": EnvSettings.era5_cds_max_retries(),

        # Parallel HTTP range requests for the RESULT transfer.  This is
        # within one file — it does NOT add CDS requests, so it is safe
        # with cds_max_concurrent = 1.  cdsapi's single stream measured
        # only ~3.5 MB/s.
        "download_connections": int(
            os.getenv("ERA5_DOWNLOAD_CONNECTIONS", "8")
        ),
    }
