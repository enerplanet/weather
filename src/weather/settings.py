"""Centralised environment settings for the weather pipeline.

All runtime configuration — paths, parallelism, authentication
endpoints, Slurm options — is exposed through :class:`EnvSettings`.
Values are read from environment variables at call time, so a change
to ``.env`` or ``os.environ`` takes effect immediately without
reimporting.

Configuration priority
----------------------
1. Environment variable (highest).
2. ``.env`` file loaded once at import time.
3. Sensible defaults coded here.

Typical usage::

    from weather.settings import EnvSettings

    print(EnvSettings.cosmo_work_dir())
    print(EnvSettings.cosmo_year())

To override for a single run without touching ``.env``::

    import os
    os.environ["COSMO_WORK_DIR"] = "/scratch/my_run"
    from weather.settings import EnvSettings
    print(EnvSettings.cosmo_work_dir())  # /scratch/my_run

``.env`` file keys (copy ``.env.example`` to ``.env`` and adjust)::

    WEATHER_DATA_DIR   # root for all provider data
    WEATHER_REGION     # single country (see geo.countries) to scope ERA5-Land
                       # and MERRA-2 WORK_DIR + AREA to automatically; does
                       # NOT apply to COSMO-REA6 (no AREA parameter exists
                       # for it) and does NOT support multiple countries --
                       # see docs/COUNTRY_SCOPED_ARCHIVES.md
    COSMO_WORK_DIR     # COSMO-REA6 working directory
    COSMO_YEAR         # four-digit year to process
    COSMO_BASE_URL     # DWD OpenData HTTPS base URL
    COSMO_NCORES       # parallel worker count
    COSMO_CLEANUP      # delete intermediates after export (default false)
    COSMO_FROM_YEAR    # multi-year run start (default 1995)
    COSMO_TO_YEAR      # multi-year run end, inclusive (default 2018)
    MERRA_WORK_DIR     # MERRA-2 working directory
    MERRA_YEAR         # MERRA-2 year
    MERRA_CLEANUP      # delete daily files after export (default false)
    MERRA_FROM_YEAR    # multi-year run start (default 1980)
    MERRA_TO_YEAR      # multi-year run end, inclusive (default 2025)
    ERA5_WORK_DIR      # ERA5-Land working directory
    ERA5_YEAR          # ERA5-Land year
    ERA5_CLEANUP       # delete GRIB after export (default false)
    ERA5_FROM_YEAR     # multi-year run start (default 1940)
    ERA5_TO_YEAR       # multi-year run end, inclusive (default 2025)

Every provider's own ``--cleanup`` CLI flag (in
``tests/test_<provider>_one_month/one_year/multi_year.py``, wired via
``common/cli_flags.add_cleanup_flag()``) defaults from exactly one of
the ``*_CLEANUP`` settings, and every
``test_<provider>_multi_year.py``'s ``--from-year``/``--to-year``
defaults from the matching ``*_FROM_YEAR``/``*_TO_YEAR`` pair -- change
the env var once and every runner picks it up; the CLI flag still
overrides it per-invocation.
"""

from __future__ import annotations

import os
from pathlib import Path

from .common.env import data_root, load_repo_env
from .geo.bbox import BBox
from .geo.countries import get_bbox, normalize_country

# Load .env exactly once at import time.
load_repo_env()


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable (``1/true/yes/on``, case-insensitive).

    Anything else (including unset) falls back to *default*.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class EnvSettings:
    """Central access point for all weather-pipeline configuration.

    Every method is a ``@staticmethod`` that reads from
    ``os.environ`` (populated by ``.env``) with a fallback default.
    Paths are returned as resolved :class:`pathlib.Path` objects.
    Scalar values are returned as their natural Python type.
    """

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    @staticmethod
    def data_dir() -> Path:
        """Root directory for all provider data (WEATHER_DATA_DIR)."""
        return data_root()

    @staticmethod
    def region_bbox() -> BBox | None:
        """Bounding box for ``WEATHER_REGION``, or ``None`` if unset.

        Raises ``ValueError`` (via :func:`~weather.geo.countries.get_bbox`)
        if set to a name not in :mod:`weather.geo.countries` -- fails fast
        on a typo rather than silently doing nothing.
        """
        region = os.getenv("WEATHER_REGION", "").strip()
        if not region:
            return None
        return get_bbox(region)

    @staticmethod
    def _region_scoped(base: Path) -> Path:
        """Append ``WEATHER_REGION``'s normalized name to *base*, if set.

        Returns *base* unchanged when unset -- today's flat layout.
        Validates the region (via :meth:`region_bbox`) before use, so an
        unknown name raises here instead of quietly creating an empty
        directory nothing ever populates.
        """
        region = os.getenv("WEATHER_REGION", "").strip()
        if not region:
            return base
        EnvSettings.region_bbox()
        return base / normalize_country(region)

    # ------------------------------------------------------------------
    # COSMO-REA6
    # ------------------------------------------------------------------

    @staticmethod
    def cosmo_work_dir() -> Path:
        """COSMO-REA6 working root (COSMO_WORK_DIR).

        Deliberately NOT scoped by ``WEATHER_REGION`` (unlike
        :meth:`era5_work_dir`/:meth:`merra2_work_dir`) -- COSMO-REA6 has
        no AREA parameter anywhere and always downloads the full European
        domain, so honoring a region here would write a whole-Europe
        archive into a directory named after one country. Use
        ``weather geo crop`` to produce a genuinely region-scoped COSMO
        file; see docs/COUNTRY_SCOPED_ARCHIVES.md.
        """
        raw = os.getenv(
            "COSMO_WORK_DIR",
            str(data_root() / "cosmo_rea6"),
        )
        return Path(raw).expanduser().resolve()

    @staticmethod
    def cosmo_download_dir() -> Path:
        """``<cosmo_work_dir>/download``."""
        return EnvSettings.cosmo_work_dir() / "download"

    @staticmethod
    def cosmo_decompress_dir() -> Path:
        """``<cosmo_work_dir>/decompress``."""
        return EnvSettings.cosmo_work_dir() / "decompress"

    @staticmethod
    def cosmo_processed_dir() -> Path:
        """``<cosmo_work_dir>/processed``."""
        return EnvSettings.cosmo_work_dir() / "processed"

    @staticmethod
    def cosmo_output_dir() -> Path:
        """``<cosmo_work_dir>/output``."""
        return EnvSettings.cosmo_work_dir() / "output"

    @staticmethod
    def cosmo_year() -> int:
        """Processing year (COSMO_YEAR, default 2018)."""
        return int(os.getenv("COSMO_YEAR", "2018"))

    @staticmethod
    def cosmo_from_year() -> int:
        """Multi-year run start (COSMO_FROM_YEAR, default 1995).

        Single source of truth for ``test_cosmo_multi_year.py``'s
        ``--from-year`` default -- change the env var once instead of
        the script's argparse default.
        """
        return int(os.getenv("COSMO_FROM_YEAR", "1995"))

    @staticmethod
    def cosmo_to_year() -> int:
        """Multi-year run end, inclusive (COSMO_TO_YEAR, default 2018).

        See :meth:`cosmo_from_year`.
        """
        return int(os.getenv("COSMO_TO_YEAR", "2018"))

    @staticmethod
    def cosmo_base_url() -> str:
        """DWD OpenData HTTPS base URL (COSMO_BASE_URL)."""
        return os.getenv(
            "COSMO_BASE_URL",
            (
                "https://opendata.dwd.de/climate_environment"
                "/REA/COSMO_REA6/hourly/2D"
            ),
        )

    @staticmethod
    def cosmo_const_url() -> str:
        """DWD OpenData constant-field URL (COSMO_CONST_URL)."""
        return os.getenv(
            "COSMO_CONST_URL",
            (
                "https://opendata.dwd.de/climate_environment"
                "/REA/COSMO_REA6/constant"
            ),
        )

    @staticmethod
    def cosmo_ncores() -> int:
        """
        Parallel worker count (COSMO_NCORES or SLURM_CPUS_PER_TASK, default 4).
        """
        return int(
            os.getenv(
                "COSMO_NCORES",
                os.getenv("SLURM_CPUS_PER_TASK", "4"),
            )
        )

    @staticmethod
    def cosmo_threads_per_job() -> int:
        """bzip2 threads per decompression job (COSMO_THREADS_PER_JOB)."""
        return int(os.getenv("COSMO_THREADS_PER_JOB", "4"))

    @staticmethod
    def cosmo_decompressor() -> str:
        """Preferred decompressor command (COSMO_DECOMPRESSOR).

        One of ``"lbzip2"``, ``"pbzip2"``, ``""`` (auto-detect).
        """
        return os.getenv("COSMO_DECOMPRESSOR", "")

    @staticmethod
    def cosmo_conda_env() -> str:
        """Conda environment name for scripts (COSMO_CONDA_ENV)."""
        return os.getenv("COSMO_CONDA_ENV", "weather_env")

    @staticmethod
    def cosmo_cleanup() -> bool:
        """Delete intermediate .bz2/.grb files after export (COSMO_CLEANUP).

        Default ``False`` (keep everything) -- single source of truth for
        every COSMO entrypoint's cleanup default (``pipeline.py``'s
        ``run_pipeline()`` and the ``test_cosmo_one_month/one_year/
        multi_year.py`` runners' ``--cleanup`` flag). Set
        ``COSMO_CLEANUP=true`` in ``.env`` to delete by default instead;
        each runner's own ``--cleanup`` CLI flag still overrides this
        per-invocation. Also read directly by the container entrypoint
        (``infrastructure/container/``) -- no separate container-level
        variable exists for this anymore.
        """
        return _env_bool("COSMO_CLEANUP", False)

    @staticmethod
    def cosmo_max_retries() -> int:
        """Retry attempts for a transient DWD download failure
        (COSMO_MAX_RETRIES, default 10).

        Single source of truth for ``downloader.py``'s call into
        ``common.download.download_https_atomic`` -- change the env var
        once instead of a hardcoded literal at the call site.
        """
        return int(os.getenv("COSMO_MAX_RETRIES", "10"))

    @staticmethod
    def cosmo_log_dir() -> Path:
        """Directory for pipeline run logs (COSMO_LOG_DIR).

        Defaults to ``<cosmo_work_dir>/logs``.  On shared HPC systems you
        may want per-user log directories; set ``COSMO_LOG_DIR`` explicitly
        in ``.env`` or the environment to override.
        """
        raw = os.getenv("COSMO_LOG_DIR", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return EnvSettings.cosmo_work_dir() / "logs"

    @staticmethod
    def cosmo_slurm_partition() -> str:
        """SLURM partition for job submission (COSMO_SLURM_PARTITION)."""
        return os.getenv("COSMO_SLURM_PARTITION", "rome")

    @staticmethod
    def cosmo_slurm_email() -> str:
        """SLURM notification e-mail (COSMO_SLURM_EMAIL)."""
        return os.getenv("COSMO_SLURM_EMAIL", "")

    # ------------------------------------------------------------------
    # MERRA-2
    # ------------------------------------------------------------------

    @staticmethod
    def merra2_work_dir() -> Path:
        """MERRA-2 working root (MERRA_WORK_DIR).

        Defaults to ``<data_root>/merra2/<region>`` when ``WEATHER_REGION``
        is set (see :meth:`_region_scoped`), else ``<data_root>/merra2`` --
        unchanged from before ``WEATHER_REGION`` existed. Explicit
        ``MERRA_WORK_DIR`` always wins over both.
        """
        raw = os.getenv("MERRA_WORK_DIR", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return EnvSettings._region_scoped(data_root() / "merra2").resolve()

    @staticmethod
    def merra2_download_dir() -> Path:
        """``<merra2_work_dir>/download``."""
        return EnvSettings.merra2_work_dir() / "download"

    @staticmethod
    def merra2_processed_dir() -> Path:
        """``<merra2_work_dir>/processed``."""
        return EnvSettings.merra2_work_dir() / "processed"

    @staticmethod
    def merra2_output_dir() -> Path:
        """``<merra2_work_dir>/output``."""
        return EnvSettings.merra2_work_dir() / "output"

    @staticmethod
    def merra2_year() -> int:
        """Processing year (MERRA_YEAR, default 2018)."""
        return int(os.getenv("MERRA_YEAR", "2018"))

    @staticmethod
    def merra2_from_year() -> int:
        """Multi-year run start (MERRA_FROM_YEAR, default 1980).

        Single source of truth for ``test_merra2_multi_year.py``'s
        ``--from-year`` default -- change the env var once instead of
        the script's argparse default. Note the ``MERRA_`` (not
        ``MERRA2_``) prefix, matching ``MERRA_WORK_DIR``/``MERRA_YEAR``.
        """
        return int(os.getenv("MERRA_FROM_YEAR", "1980"))

    @staticmethod
    def merra2_to_year() -> int:
        """Multi-year run end, inclusive (MERRA_TO_YEAR, default 2025).

        See :meth:`merra2_from_year`.
        """
        return int(os.getenv("MERRA_TO_YEAR", "2025"))

    @staticmethod
    def merra2_ncores() -> int:
        """Parallel worker count (MERRA_NCORES or SLURM_CPUS_PER_TASK)."""
        return int(
            os.getenv(
                "MERRA_NCORES",
                os.getenv(
                    "SLURM_CPUS_PER_TASK",
                    str(os.cpu_count() or 4),
                ),
            )
        )

    @staticmethod
    def merra2_threads_per_job() -> int:
        """Threads per job (MERRA_THREADS_PER_JOB)."""
        return int(os.getenv("MERRA_THREADS_PER_JOB", "4"))

    @staticmethod
    def merra2_conda_env() -> str:
        """Conda environment name (MERRA_CONDA_ENV)."""
        return os.getenv("MERRA_CONDA_ENV", "weather_env")

    @staticmethod
    def merra2_cleanup() -> bool:
        """Delete downloaded daily files after export (MERRA_CLEANUP).

        Default ``False`` (keep everything) -- single source of truth for
        every MERRA-2 entrypoint's cleanup default (``pipeline.py``'s
        ``run_pipeline()`` and the ``test_merra2_one_month/one_year/
        multi_year.py`` runners' ``--cleanup`` flag). Set
        ``MERRA_CLEANUP=true`` in ``.env`` to delete by default instead;
        each runner's own ``--cleanup`` CLI flag still overrides this
        per-invocation.
        """
        return _env_bool("MERRA_CLEANUP", False)

    @staticmethod
    def merra2_slurm_partition() -> str:
        """SLURM partition (MERRA_SLURM_PARTITION)."""
        return os.getenv("MERRA_SLURM_PARTITION", "rome")

    @staticmethod
    def merra2_slurm_email() -> str:
        """SLURM notification e-mail (MERRA_SLURM_EMAIL)."""
        return os.getenv("MERRA_SLURM_EMAIL", "")

    @staticmethod
    def merra2_area() -> list[float]:
        """Geographic bounding box for OPeNDAP requests (MERRA2_AREA).

        Order is ``[North, West, South, East]`` in degrees. Required --
        unset or blank raises, rather than silently defaulting to a
        whole-Europe download (see docs/COUNTRY_SCOPED_ARCHIVES.md for
        computing the box for a country set).
        """
        raw = os.getenv("MERRA2_AREA", "").strip()
        if not raw:
            raise ValueError(
                "MERRA2_AREA is required (format 'N,W,S,E') -- refusing to "
                "default to a whole-Europe download."
            )
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError(
                f"MERRA2_AREA must be 'N,W,S,E' (4 numbers), got: {raw!r}"
            )
        try:
            return [float(p) for p in parts]
        except ValueError as exc:
            raise ValueError(
                f"MERRA2_AREA values must be numeric, got: {raw!r}"
            ) from exc

    @staticmethod
    def merra2_opendap_max_concurrent() -> int:
        """Parallel OPeNDAP requests (MERRA2_OPENDAP_MAX_CONCURRENT).

        Unlike ERA5-Land's CDS queue (effectively 1 job/account), GES
        DISC's OPeNDAP server has no per-account job queue, so this can
        default higher.
        """
        return int(os.getenv("MERRA2_OPENDAP_MAX_CONCURRENT", "8"))

    @staticmethod
    def merra2_opendap_max_retries() -> int:
        """Retry attempts for a transient GES DISC failure
        (MERRA2_OPENDAP_MAX_RETRIES, default 10).

        Single source of truth for ``downloader.py``'s
        ``exponential_backoff`` call -- change the env var once instead
        of a hardcoded literal at the call site.
        """
        return int(os.getenv("MERRA2_OPENDAP_MAX_RETRIES", "10"))

    # ------------------------------------------------------------------
    # ERA5-Land
    # ------------------------------------------------------------------

    @staticmethod
    def era5_work_dir() -> Path:
        """ERA5-Land working root (ERA5_WORK_DIR).

        Defaults to ``<data_root>/era5_land/<region>`` when
        ``WEATHER_REGION`` is set (see :meth:`_region_scoped`), else
        ``<data_root>/era5_land`` -- unchanged from before
        ``WEATHER_REGION`` existed. Explicit ``ERA5_WORK_DIR`` always wins
        over both.
        """
        raw = os.getenv("ERA5_WORK_DIR", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return EnvSettings._region_scoped(data_root() / "era5_land").resolve()

    @staticmethod
    def era5_download_dir() -> Path:
        """``<era5_work_dir>/download``."""
        return EnvSettings.era5_work_dir() / "download"

    @staticmethod
    def era5_processed_dir() -> Path:
        """``<era5_work_dir>/processed``."""
        return EnvSettings.era5_work_dir() / "processed"

    @staticmethod
    def era5_output_dir() -> Path:
        """``<era5_work_dir>/output``."""
        return EnvSettings.era5_work_dir() / "output"

    @staticmethod
    def era5_year() -> int:
        """Processing year (ERA5_YEAR, default 2018)."""
        return int(os.getenv("ERA5_YEAR", "2018"))

    @staticmethod
    def era5_from_year() -> int:
        """Multi-year run start (ERA5_FROM_YEAR, default 1940).

        Single source of truth for ``test_era5_multi_year.py``'s
        ``--from-year`` default -- change the env var once instead of
        the script's argparse default.
        """
        return int(os.getenv("ERA5_FROM_YEAR", "1940"))

    @staticmethod
    def era5_to_year() -> int:
        """Multi-year run end, inclusive (ERA5_TO_YEAR, default 2025).

        See :meth:`era5_from_year`.
        """
        return int(os.getenv("ERA5_TO_YEAR", "2025"))

    @staticmethod
    def era5_ncores() -> int:
        """Parallel worker count (ERA5_NCORES or SLURM_CPUS_PER_TASK)."""
        return int(
            os.getenv(
                "ERA5_NCORES",
                os.getenv(
                    "SLURM_CPUS_PER_TASK",
                    str(os.cpu_count() or 4),
                ),
            )
        )

    @staticmethod
    def era5_threads_per_job() -> int:
        """Threads per job (ERA5_THREADS_PER_JOB)."""
        return int(os.getenv("ERA5_THREADS_PER_JOB", "4"))

    @staticmethod
    def era5_conda_env() -> str:
        """Conda environment name (ERA5_CONDA_ENV)."""
        return os.getenv("ERA5_CONDA_ENV", "weather_env")

    @staticmethod
    def era5_cleanup() -> bool:
        """Delete the downloaded GRIB after export (ERA5_CLEANUP).

        Default ``False`` (keep everything) -- single source of truth for
        every ERA5-Land entrypoint's cleanup default (``pipeline.py``'s
        ``run_pipeline()`` and the ``test_era5_one_month/one_year/
        multi_year.py`` runners' ``--cleanup`` flag). Set
        ``ERA5_CLEANUP=true`` in ``.env`` to delete by default instead;
        each runner's own ``--cleanup`` CLI flag still overrides this
        per-invocation.
        """
        return _env_bool("ERA5_CLEANUP", False)

    @staticmethod
    def era5_cds_dataset() -> str:
        """CDS dataset id for ERA5-Land (ERA5_CDS_DATASET)."""
        return os.getenv("ERA5_CDS_DATASET", "reanalysis-era5-land")

    @staticmethod
    def era5_data_format() -> str:
        """CDS response data format (ERA5_DATA_FORMAT).

        Typical values: ``"grib"`` (recommended), ``"netcdf"``.
        """
        return os.getenv("ERA5_DATA_FORMAT", "grib")

    @staticmethod
    def era5_download_format() -> str:
        """CDS download packaging format (ERA5_DOWNLOAD_FORMAT)."""
        return os.getenv("ERA5_DOWNLOAD_FORMAT", "unarchived")

    @staticmethod
    def era5_cds_url() -> str:
        """CDS API URL override (ERA5_CDS_URL)."""
        return os.getenv("ERA5_CDS_URL", "").strip()

    @staticmethod
    def era5_cds_key() -> str:
        """CDS API key override (ERA5_CDS_KEY).

        Prefer ``~/.cdsapirc``.  Use this only in controlled
        environments such as CI secrets or scheduler exports.
        """
        return os.getenv("ERA5_CDS_KEY", "").strip()

    @staticmethod
    def era5_cds_max_retries() -> int:
        """Retry attempts for a transient CDS failure
        (ERA5_CDS_MAX_RETRIES, default 10).

        Single source of truth for ``downloader.py``'s retry loop --
        previously read directly via ``os.getenv`` in ``config.py``
        (default 5); centralized here to match COSMO's and MERRA-2's
        equivalent knobs.
        """
        return int(os.getenv("ERA5_CDS_MAX_RETRIES", "10"))

    @staticmethod
    def era5_slurm_partition() -> str:
        """SLURM partition (ERA5_SLURM_PARTITION)."""
        return os.getenv("ERA5_SLURM_PARTITION", "rome")

    @staticmethod
    def era5_slurm_email() -> str:
        """SLURM notification e-mail (ERA5_SLURM_EMAIL)."""
        return os.getenv("ERA5_SLURM_EMAIL", "")
