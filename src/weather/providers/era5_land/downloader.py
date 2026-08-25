"""ERA5-Land concrete downloader via the Copernicus CDS API.

Implements :class:`~weather.providers.base_downloader.BaseDownloader` for
the Copernicus Climate Data Store (CDS) ERA5-Land dataset.

Authentication
--------------
Requires a free Copernicus account and a Personal Access Token:

1. Register at https://cds.climate.copernicus.eu/
2. Accept the ERA5-Land licence on the dataset page.
3. Either create ``~/.cdsapirc``::

       url: https://cds.climate.copernicus.eu/api
       key: <your-personal-access-token>

   or set ``ERA5_CDS_URL`` / ``ERA5_CDS_KEY`` in ``.env`` (these take
   precedence when both are non-empty).
4. ``conda install -c conda-forge cdsapi``

Download model
--------------
* **All-variables-per-month**: one CDS request fetches every configured
  variable for one month into a single GRIB.  The sentinel attribute
  :data:`ALL_ATTRS` lets this fit the
  :class:`~.base_downloader.DownloadJob` model (one job = one month).
* **Queue-based**: CDS processes a limited number of jobs per account
  (empirically ONE at a time), queueing the rest.  ``remote_size``
  therefore returns ``None`` and completeness is decided by local-file
  existence.
* **Geographic crop**: an optional ``area`` ([N, W, S, E]) shrinks each
  GRIB message dramatically, which both cuts storage and avoids eccodes
  memory-allocation failures on the full global grid.
* **Parallel transfer**: the result is pulled with concurrent HTTP range
  requests (see :mod:`~weather.providers.era5_land.fast_download`),
  because ``cdsapi``'s own ``result.download()`` is single-stream and
  measured at only ~3.5 MB/s.  This is parallelism WITHIN one file's
  transfer — it does NOT add CDS requests, so it is safe alongside
  ``cds_max_concurrent = 1``.
"""

from __future__ import annotations

import calendar
import logging
import random
import time
from pathlib import Path
from typing import Any

from ..base_downloader import BaseDownloader, DownloadJob

logger = logging.getLogger(__name__)

#: Sentinel ``DownloadJob.attribute`` meaning "all configured variables
#: in a single monthly request/file".
ALL_ATTRS: str = "all_attrs"

#: ``.env`` placeholders that must not be sent to CDS as a real key.
_PLACEHOLDERS = frozenset({
    "your_cds_api_key_here",
    "<your-personal-access-token>",
    "your_cds_api_key",
})


class Era5Downloader(BaseDownloader):
    """ERA5-Land downloader via the Copernicus CDS API.

    Parameters
    ----------
    config : dict
        As returned by
        :func:`~weather.providers.era5_land.config.get_config`.
        Required: ``download_dir``, ``attributes``, ``dataset``,
        ``data_format``, ``download_format``.
        Optional: ``cds_url``, ``cds_key``, ``area``,
        ``cds_max_retries``, ``download_connections``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config

    # ------------------------------------------------------------------
    # BaseDownloader implementation
    # ------------------------------------------------------------------

    def remote_size(self, job: DownloadJob) -> int | None:
        """Always ``None``: CDS cannot report a size before processing."""
        return None

    def _ext(self) -> str:
        ext = str(self._cfg.get("data_format", "grib")).lower().strip()
        return "nc" if ext in ("netcdf", "nc") else "grib"

    def local_path(self, job: DownloadJob) -> Path:
        """``<download_dir>/ERA5_LAND_<YYYY>_<MM>_all_attrs.<ext>``, or
        ``ERA5_LAND_<TAG>_<YYYY>_<MM>_all_attrs.<ext>`` when
        ``cfg["region_tag"]`` is set (see :meth:`content_key`'s
        docstring for why this alone isn't sufficient protection)."""
        tag = self._cfg.get("region_tag")
        tag_part = f"{tag}_" if tag else ""
        fname = (
            f"ERA5_LAND_{tag_part}{job.year}_{job.month:02d}"
            f"_all_attrs.{self._ext()}"
        )
        return self._cfg["download_dir"] / fname

    def content_key(self, job: DownloadJob) -> str | None:
        """The configured area, as a stable string -- ``"GLOBAL"`` when
        unset. Two different areas for the same year/month must never
        be treated as the same downloaded content, even if they'd
        otherwise share a :meth:`local_path` (e.g. two different
        callers that both leave ``region_tag`` unset, such as the
        documented ``export ERA5_AREA=...`` bulk-run workflow)."""
        area = self._cfg.get("area")
        return ",".join(str(v) for v in area) if area else "GLOBAL"

    def _build_request(self, job: DownloadJob) -> dict[str, Any]:
        """CDS request for one month, all configured variables."""
        days_in_month = calendar.monthrange(job.year, job.month)[1]
        request: dict[str, Any] = {
            "variable": list(self._cfg["attributes"]),
            "year": str(job.year),
            "month": f"{job.month:02d}",
            "day": [f"{d:02d}" for d in range(1, days_in_month + 1)],
            "time": [f"{h:02d}:00" for h in range(24)],
            "data_format": self._cfg.get("data_format", "grib"),
            "download_format": self._cfg.get(
                "download_format", "unarchived"
            ),
        }
        # Optional crop: CDS 'area' = [North, West, South, East].
        area = self._cfg.get("area")
        if area:
            request["area"] = list(area)
        return request

    def _client(self):
        """Build a cdsapi client, guarding against the .env placeholder."""
        try:
            import cdsapi
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "cdsapi is required for ERA5-Land downloads. "
                "Install with: conda install -c conda-forge cdsapi"
            ) from exc

        cds_url = str(self._cfg.get("cds_url", "")).strip()
        cds_key = str(self._cfg.get("cds_key", "")).strip()

        if cds_key in _PLACEHOLDERS:
            logger.warning(
                "ERA5_CDS_KEY is still the placeholder value; ignoring it "
                "and falling back to ~/.cdsapirc.  Set a real key in .env "
                "or leave ERA5_CDS_KEY empty."
            )
            cds_key = ""

        kwargs = (
            {"url": cds_url, "key": cds_key} if (cds_url and cds_key) else {}
        )
        return cdsapi.Client(**kwargs)

    @staticmethod
    def _result_url(result) -> str | None:
        """Extract the object-store URL from a cdsapi result object.

        cdsapi has renamed this across versions, so try the known
        attributes in turn.  Returning ``None`` makes the caller fall back
        to ``result.download()``.
        """
        for attr in ("location", "url"):
            url = getattr(result, attr, None)
            if isinstance(url, str) and url.startswith("http"):
                return url
        # Newer ecmwf-datastores clients expose it via get_results().
        try:
            res = result.get_results()
            url = getattr(res, "location", None)
            if isinstance(url, str) and url.startswith("http"):
                return url
        except Exception:  # noqa: BLE001
            pass
        return None

    def _download_result(self, result, tmp: Path) -> None:
        """Pull the finished CDS result into *tmp*.

        Prefers the parallel range-request downloader; falls back to
        cdsapi's own single-stream ``download()`` if the URL cannot be
        determined or the fast path fails.
        """
        connections = int(self._cfg.get("download_connections", 8))

        url = self._result_url(result)
        if url and connections > 1:
            try:
                from .fast_download import download_parallel

                download_parallel(url, tmp, connections=connections)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Parallel download failed (%s); falling back to "
                    "cdsapi single-stream.", exc,
                )
        else:
            logger.debug(
                "No result URL exposed (or connections=1); using cdsapi "
                "single-stream download."
            )

        result.download(str(tmp))

    def _fetch(self, job: DownloadJob) -> Path:
        """Submit one monthly CDS request and download the result.

        Retries with exponential backoff: over a multi-week unattended
        run a transient CDS failure must not kill a month.
        """
        request = self._build_request(job)
        client = self._client()
        dataset = str(
            self._cfg.get("dataset", "reanalysis-era5-land")
        ).strip()

        dest = self.local_path(job)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        if tmp.exists():
            tmp.unlink()

        logger.info(
            "ERA5 CDS request: %04d-%02d  %d variables  (%s)%s",
            job.year, job.month, len(request["variable"]),
            self._cfg.get("data_format", "grib"),
            f"  area={request['area']}" if "area" in request else "",
        )

        max_attempts = int(self._cfg.get("cds_max_retries", 10))
        base_delay, max_delay = 30.0, 900.0
        last_exc: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = client.retrieve(dataset, request)
                self._download_result(result, tmp)

                if not tmp.exists() or tmp.stat().st_size == 0:
                    raise RuntimeError(
                        f"Empty ERA5-Land download for {job}: {tmp}"
                    )
                tmp.replace(dest)
                logger.info(
                    "ERA5 download complete: %s (%.1f MB)",
                    dest.name, dest.stat().st_size / (1024 * 1024),
                )
                return dest

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                tmp.unlink(missing_ok=True)
                if attempt == max_attempts:
                    break
                delay = min(
                    max_delay, base_delay * 2 ** (attempt - 1)
                ) + random.uniform(0, 15)
                logger.warning(
                    "CDS attempt %d/%d failed for %s (%s); retrying in "
                    "%.0f s", attempt, max_attempts, job, exc, delay,
                )
                time.sleep(delay)

        raise RuntimeError(
            f"ERA5-Land download failed for {job} after {max_attempts} "
            f"attempts: {last_exc}"
        )
