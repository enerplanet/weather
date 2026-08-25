"""MERRA-2 concrete downloader via NASA GES DISC OPeNDAP.

Implements :class:`~weather.providers.base_downloader.BaseDownloader` for
the NASA GES DISC MERRA-2 archive, using **OPeNDAP** server-side
subsetting rather than full-file HTTPS download — only the configured
Europe bounding box is ever transferred, never a global daily file.

Authentication
--------------
Requires a free NASA Earthdata account:

1. Register at https://urs.earthdata.nasa.gov/
2. In *Applications > Authorized Apps*, add **"NASA GESDISC DATA
   ARCHIVE"** to your approved list.
3. Either set ``EARTHDATA_USERNAME``/``EARTHDATA_PASSWORD`` in ``.env``,
   or create ``~/.netrc``::

       machine urs.earthdata.nasa.gov
           login  <your_username>
           password <your_password>

   (see :func:`weather.common.net.earthdata_auth`).

NASA's login flow redirects across hosts (``urs.earthdata.nasa.gov`` <->
the GES DISC data host), and ``requests`` strips the ``Authorization``
header on such cross-host redirects by default — so a plain session
would silently fail auth partway through.  :func:`_session` uses
:func:`weather.common.net.build_session`'s ``preserve_auth_hosts`` to
keep the header attached across that specific redirect chain.

Download model
---------------
* **All-attributes-per-collection-per-day**: one OPeNDAP request fetches
  every configured attribute of one collection (see
  :data:`~weather.providers.merra2.downloaded_attributes.COLLECTIONS`)
  for one calendar day, cropped to
  :func:`~weather.providers.merra2.config`'s
  ``area`` (Europe by default).  MERRA-2 files are per-DAY (unlike
  COSMO's per-month), so :class:`Merra2DownloadJob` carries a ``day``
  field — see :mod:`weather.providers.base_downloader`'s own docstring,
  which names MERRA-2 as the example provider needing this.
* **No queue**: unlike CDS, GES DISC's OPeNDAP server has no
  per-account job queue, so ``remote_size`` and retries are plain
  HTTP semantics (see
  :data:`weather.settings.EnvSettings.merra2_opendap_max_concurrent`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ...common.net import build_session, earthdata_auth, exponential_backoff
from ..base_downloader import BaseDownloader
from .downloaded_attributes import ATTRIBUTES, COLLECTIONS, attrs_by_collection

logger = logging.getLogger(__name__)

#: OPeNDAP host serving MERRA-2 collections.
_GES_DISC_HOST = "goldsmr4.gesdisc.eosdis.nasa.gov"
_OPENDAP_BASE = f"https://{_GES_DISC_HOST}/opendap/MERRA2"

#: Hosts that must keep Authorization across NASA's login redirect chain.
_TRUSTED_AUTH_HOSTS = frozenset({
    "urs.earthdata.nasa.gov",
    _GES_DISC_HOST,
})

#: GES DISC file "product" token per collection (fixed by NASA's naming).
_PRODUCT_TOKEN = {
    "rad": "tavg1_2d_rad_Nx",
    "slv": "tavg1_2d_slv_Nx",
    "lnd": "tavg1_2d_lnd_Nx",
}

#: MERRA-2 "stream" number prefix by date range (fixed by NASA's naming;
#: see https://disc.gsfc.nasa.gov/information/glossary?title=
#: MERRA-2%20File%20Naming%20Conventions). NASA occasionally reprocesses
#: a handful of months under a bumped runid (confirmed live: Sep 2020 and
#: Jun-Sep 2021 are served as stream 401, not 400) -- these reprocessed
#: windows are scattered, not a clean date range, so ``_fetch`` falls back
#: from this primary stream to ``primary + 1`` on a 404 instead of trying
#: to hardcode the affected months here.
_STREAM_RANGES: tuple[tuple[int, int, int], ...] = (
    (1980, 1991, 100),
    (1992, 2000, 200),
    (2001, 2010, 300),
    (2011, 9999, 400),
)


class _StreamNotFound(Exception):
    """Raised when a MERRA-2 stream candidate 404s (plain Exception, not
    OSError, so :func:`~weather.common.net.exponential_backoff` won't
    burn retries on what is a permanent, not transient, failure)."""


#: MERRA-2's fixed global grid (0.5 deg lat x 0.625 deg lon, origin at
#: the South-West corner, -90/-180).
_LAT_STEP = 0.5
_LON_STEP = 0.625
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LON_MIN, _LON_MAX = -180.0, 180.0


def _stream_prefix(year: int) -> int:
    """Return the MERRA-2 stream number (100/200/300/400) for *year*."""
    for lo, hi, stream in _STREAM_RANGES:
        if lo <= year <= hi:
            return stream
    raise ValueError(f"No MERRA-2 stream defined for year {year}")


def _bbox_indices(area: list[float]) -> tuple[int, int, int, int]:
    """Convert a ``[N, W, S, E]`` box to MERRA-2 native grid indices.

    Parameters
    ----------
    area : list[float]
        ``[North, West, South, East]`` in degrees, as returned by
        :meth:`weather.settings.EnvSettings.merra2_area`.

    Returns
    -------
    (lat0, lat1, lon0, lon1) : tuple[int, int, int, int]
        Inclusive 0-based index ranges into MERRA-2's fixed global grid
        (361 lat points, 576 lon points), lat ascending south-to-north,
        lon ascending west-to-east.
    """
    north, west, south, east = area
    lat0 = math.floor((south - _LAT_MIN) / _LAT_STEP)
    lat1 = math.ceil((north - _LAT_MIN) / _LAT_STEP)
    lon0 = math.floor((west - _LON_MIN) / _LON_STEP)
    lon1 = math.ceil((east - _LON_MIN) / _LON_STEP)
    n_lat = round((_LAT_MAX - _LAT_MIN) / _LAT_STEP)
    n_lon = round((_LON_MAX - _LON_MIN) / _LON_STEP)
    lat0, lat1 = max(0, lat0), min(n_lat, lat1)
    lon0, lon1 = max(0, lon0), min(n_lon, lon1)
    return lat0, lat1, lon0, lon1


@dataclass(frozen=True)
class Merra2DownloadJob:
    """Coordinates identifying one MERRA-2 OPeNDAP request.

    Unlike COSMO/ERA5-Land's ``DownloadJob(attribute, year, month)``,
    MERRA-2 files are per-DAY and one request already covers every
    attribute of a *collection* (see
    :func:`~weather.providers.merra2.downloaded_attributes.
    attrs_by_collection`), so the job is keyed by
    ``(collection, year, month, day)`` rather than
    by individual attribute.  Sanctioned by
    :mod:`weather.providers.base_downloader`'s own docstring, which
    names MERRA-2 as the provider expected to define its own job type.

    Attributes
    ----------
    collection : str
        Short collection key (a key of
        :data:`~weather.providers.merra2.downloaded_attributes.COLLECTIONS`,
        currently ``"rad"``, ``"slv"``, or ``"lnd"``).
    year, month, day : int
        Calendar date this request covers.
    """

    collection: str
    year: int
    month: int
    day: int

    def __str__(self) -> str:
        return f"{self.collection} {self.year}-{self.month:02d}-{self.day:02d}"


class Merra2Downloader(BaseDownloader):
    """MERRA-2 downloader for NASA GES DISC OPeNDAP.

    Parameters
    ----------
    config : dict
        Pipeline configuration as returned by
        :func:`~weather.providers.merra2.config.get_config`.
        Required keys: ``download_dir``, ``area``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._session: Any = None

    # ------------------------------------------------------------------
    # Session / URL construction
    # ------------------------------------------------------------------

    def _get_session(self) -> Any:
        if self._session is None:
            self._session = build_session(
                auth=earthdata_auth(),
                preserve_auth_hosts=_TRUSTED_AUTH_HOSTS,
            )
        return self._session

    def build_url(self, job: Merra2DownloadJob, stream: int | None = None) -> str:
        """Return the full OPeNDAP constraint URL for *job*.

        Pure string/index math — no network I/O — so it can be
        unit-tested and inspected without Earthdata credentials.

        Parameters
        ----------
        stream : int | None
            Explicit MERRA-2 stream number, overriding the year-derived
            default (see :func:`_stream_prefix`) — used by :meth:`_fetch`
            to retry a reprocessed-month 404 under the next stream.
        """
        collection_id = COLLECTIONS[job.collection]
        product = _PRODUCT_TOKEN[job.collection]
        if stream is None:
            stream = _stream_prefix(job.year)
        date_str = f"{job.year}{job.month:02d}{job.day:02d}"
        filename = f"MERRA2_{stream}.{product}.{date_str}.nc4"

        area = self._cfg["area"]
        lat0, lat1, lon0, lon1 = _bbox_indices(area)

        attrs = attrs_by_collection()[job.collection]
        var_terms = [
            f"{ATTRIBUTES[a]['m2_name']}"
            f"[0:1:23][{lat0}:1:{lat1}][{lon0}:1:{lon1}]"
            for a in attrs
        ]
        coord_terms = [
            "time",
            f"lat[{lat0}:1:{lat1}]",
            f"lon[{lon0}:1:{lon1}]",
        ]
        constraint = ",".join(var_terms + coord_terms)

        # NB: the trailing ".nc4" is intentional and NOT a duplicate typo
        # — GES DISC's OPeNDAP data-access endpoint appends an output-
        # format suffix on top of the granule's own ".nc4" filename, so
        # the real URL ends in ".nc4.nc4" (confirmed against NASA's own
        # published example URLs).
        base = (
            f"{_OPENDAP_BASE}/{collection_id}/{job.year}/{job.month:02d}/"
            f"{filename}.nc4"
        )
        return f"{base}?{quote(constraint, safe='[],:')}"

    # ------------------------------------------------------------------
    # BaseDownloader implementation
    # ------------------------------------------------------------------

    # NB: these 3 methods intentionally take Merra2DownloadJob, not the
    # base DownloadJob (attribute, year, month) — MERRA-2's per-day, per-
    # collection granularity doesn't fit that shape.  base_downloader.py's
    # own docstring sanctions this ("providers with a different
    # granularity ... define their own job type"), so the resulting
    # mypy Liskov warning is expected and intentionally suppressed below.

    def local_path(  # type: ignore[override]
        self, job: Merra2DownloadJob,
    ) -> Path:
        """Return the local destination path for *job*.

        ``MERRA2_<collection>_<TAG>_<YYYYMMDD>.nc4`` when
        ``cfg["region_tag"]`` is set, else the untagged
        ``MERRA2_<collection>_<YYYYMMDD>.nc4`` (byte-identical to
        before this tag existed). See :meth:`content_key`'s docstring
        for why this alone isn't sufficient protection.
        """
        date_str = f"{job.year}{job.month:02d}{job.day:02d}"
        download_dir = Path(self._cfg["download_dir"])
        tag = self._cfg.get("region_tag")
        tag_part = f"{tag}_" if tag else ""
        return download_dir / f"MERRA2_{job.collection}_{tag_part}{date_str}.nc4"

    def content_key(  # type: ignore[override]
        self, job: Merra2DownloadJob,
    ) -> str | None:
        """The configured area, as a stable string. MERRA-2's ``area``
        always has at least the Europe default (never falsy), unlike
        ERA5-Land's global-by-default. Two different areas for the same
        collection/day must never be treated as the same downloaded
        content, even if they'd otherwise share a :meth:`local_path`
        (e.g. two different callers that both leave ``region_tag``
        unset)."""
        return ",".join(str(v) for v in self._cfg["area"])

    def remote_size(  # type: ignore[override]
        self, job: Merra2DownloadJob,
    ) -> int | None:
        """Always ``None``.

        OPeNDAP subset responses don't reliably report a size before
        the transfer completes across all THREDDS/Hyrax server
        configurations, so completeness is existence-based — the same
        rationale
        :class:`~weather.providers.era5_land.downloader.Era5Downloader`
        uses for the CDS queue.
        """
        return None

    def _fetch(  # type: ignore[override]
        self, job: Merra2DownloadJob,
    ) -> Path:
        """Download *job* via an authenticated, redirect-safe session.

        Tries the year's primary stream first, falling back to the next
        stream number on a 404 (see :data:`_STREAM_RANGES`'s docstring —
        NASA sometimes reprocesses a month under a bumped runid). A 404
        is treated as immediately-fatal-for-this-stream (via
        :class:`_StreamNotFound`, not ``OSError``) so it moves to the next
        candidate right away instead of burning several backoff retries
        on a permanent failure; transient errors (503, timeouts, ...)
        still retry against the same stream as before.
        """
        dest = self.local_path(job)
        dest.parent.mkdir(parents=True, exist_ok=True)
        session = self._get_session()
        primary = _stream_prefix(job.year)
        candidates = (primary, primary + 1)
        max_retries = self._cfg.get("opendap_max_retries", 10)

        @exponential_backoff(max_attempts=max_retries, exceptions=(OSError,))
        def _download(url: str) -> Path:
            tmp = dest.with_suffix(dest.suffix + ".part")
            logger.info("Fetching %s -> %s", job, dest.name)
            with session.get(url, stream=True, timeout=(10, 300)) as resp:
                if resp.status_code == 404:
                    raise _StreamNotFound(url)
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
            if tmp.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
                raise OSError(f"Downloaded file is empty: {url}")
            tmp.replace(dest)
            return dest

        for i, stream in enumerate(candidates):
            url = self.build_url(job, stream=stream)
            try:
                return _download(url)
            except _StreamNotFound:
                if i == len(candidates) - 1:
                    raise OSError(
                        f"No MERRA-2 stream found for {job} "
                        f"(tried streams {candidates})"
                    ) from None
                logger.info(
                    "%s: stream %d not found (404), retrying as stream %d",
                    job, stream, candidates[i + 1],
                )
        raise AssertionError("unreachable")  # pragma: no cover
