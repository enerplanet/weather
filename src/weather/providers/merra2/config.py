"""MERRA-2 pipeline configuration.

All settings are resolved through
:class:`~weather.settings.EnvSettings`.

Typical usage::

    from weather.providers.merra2.config import get_config
    cfg = get_config()
    print(cfg["work_dir"])
"""

from __future__ import annotations

from typing import Any

from ...settings import EnvSettings
from .downloaded_attributes import ATTRIBUTES


def get_config() -> dict[str, Any]:
    """Return the fully-resolved MERRA-2 configuration.

    Returns
    -------
    dict[str, Any]
        Keys: ``work_dir``, ``download_dir``, ``processed_dir``,
        ``output_dir``, ``year``, ``attributes``, ``ncores``,
        ``threads_per_job``, ``conda_env``, ``cleanup``, ``area``,
        ``opendap_max_concurrent``.
    """
    return {
        "work_dir": EnvSettings.merra2_work_dir(),
        "download_dir": EnvSettings.merra2_download_dir(),
        "processed_dir": EnvSettings.merra2_processed_dir(),
        "output_dir": EnvSettings.merra2_output_dir(),
        "year": EnvSettings.merra2_year(),
        "attributes": list(ATTRIBUTES.keys()),
        "ncores": EnvSettings.merra2_ncores(),
        "threads_per_job": EnvSettings.merra2_threads_per_job(),
        "conda_env": EnvSettings.merra2_conda_env(),
        "cleanup": EnvSettings.merra2_cleanup(),
        # Geographic crop [N, W, S, E]; required, see
        # EnvSettings.merra2_area.
        "area": EnvSettings.merra2_area(),
        # GES DISC OPeNDAP has no CDS-style per-account job queue, so
        # this can be higher than ERA5's cds_max_concurrent=1.
        "opendap_max_concurrent": EnvSettings.merra2_opendap_max_concurrent(),
        "opendap_max_retries": EnvSettings.merra2_opendap_max_retries(),
    }
