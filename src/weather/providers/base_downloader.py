"""Abstract base class for weather-data downloaders.

Each weather data provider has a concrete subclass:

+----------------+--------------------------------------------+
| Provider       | Concrete subclass                          |
+================+============================================+
| COSMO-REA6     | ``cosmo_rea6.downloader.CosmoDownloader``  |
+----------------+--------------------------------------------+
| MERRA-2        | ``merra2.downloader.Merra2Downloader``     |
+----------------+--------------------------------------------+
| ERA5-Land      | ``era5_land.downloader.Era5Downloader``    |
+----------------+--------------------------------------------+

Template-method workflow (see :meth:`BaseDownloader.get`)::

    is_complete(job)?
        yes  -> return cached local_path(job)   # zero network I/O
        no   -> _fetch(job)                      # provider-specific

Subclasses must implement three abstract methods:

* :meth:`remote_size` — query file size on the remote source.
* :meth:`local_path`  — resolve the expected local destination path.
* :meth:`_fetch`      — perform the actual download.

The non-abstract :meth:`is_complete` and :meth:`get` provide the
shared skip-if-already-downloaded logic that is identical for every
provider.

:meth:`content_key` is a fourth, *non-abstract* hook (default ``None``)
that subclasses can override when :meth:`local_path` alone doesn't
uniquely identify what's actually in the file — e.g. ERA5-Land/MERRA-2,
where the same ``(year, month[, day])`` can be requested with a
different server-side area crop (``ERA5_AREA``/``MERRA2_AREA``), so two
different areas' downloads collide on the same filename. When
overridden, :meth:`is_complete`/:meth:`get` maintain a small sidecar
file recording the content key actually used, and treat an existing
file as complete only if no sidecar is present (trust local, exactly as
before) or the sidecar matches the currently requested key (a mismatch
forces a real re-fetch instead of silently reusing stale data from a
different area). COSMO-REA6 never overrides this — DWD always serves
the whole fixed domain regardless of any crop request, so its
downloaded file is correct content for every possible request; leaving
:meth:`content_key` at its default ``None`` is a hard no-op, verified
in ``tests/test_base_downloader.py``.

``DownloadJob`` is a lightweight frozen dataclass that bundles the
three coordinates that identify a single downloadable file for
attribute-per-month providers (COSMO-REA6).  Providers with a
different granularity (e.g. MERRA-2 daily files) define their own
job type and annotate their subclass accordingly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DownloadJob:
    """Coordinates identifying one downloadable file.

    Attributes
    ----------
    attribute : str
        Variable name (e.g. ``"SWDIRS_RAD"``).
    year : int
        Four-digit year.
    month : int
        Month (1–12).
    """

    attribute: str
    year: int
    month: int

    def __str__(self) -> str:
        return (
            f"{self.attribute} "
            f"{self.year}-{self.month:02d}"
        )


class BaseDownloader(ABC):
    """Template-method base class for provider file downloaders.

    Concrete subclasses implement the three abstract methods below.
    The public :meth:`get` method orchestrates the full
    check-then-fetch workflow and is shared by all providers.

    Examples
    --------
    Typical subclass skeleton::

        class MyDownloader(BaseDownloader):
            def remote_size(self, job):
                ...  # HEAD request or catalog lookup

            def local_path(self, job):
                ...  # provider-specific path layout

            def _fetch(self, job):
                ...  # actual download to local_path(job)
    """

    # ------------------------------------------------------------------
    # Abstract interface — subclasses must override
    # ------------------------------------------------------------------

    @abstractmethod
    def remote_size(self, job: DownloadJob) -> int | None:
        """Return the expected file size on the remote source.

        Parameters
        ----------
        job : DownloadJob
            Download coordinates.

        Returns
        -------
        int or None
            File size in bytes, or ``None`` when the remote source
            cannot report a size before the download is initiated
            (e.g. CDS API queue-based responses).
        """

    @abstractmethod
    def local_path(self, job: DownloadJob) -> Path:
        """Return the expected local destination path for *job*.

        The parent directory need not exist yet; :meth:`_fetch` is
        responsible for creating it.

        Parameters
        ----------
        job : DownloadJob
            Download coordinates.
        """

    @abstractmethod
    def _fetch(self, job: DownloadJob) -> Path:
        """Execute the provider-specific download.

        This method is called *only* when :meth:`is_complete` returns
        ``False``.  Implementations should write to a temporary file
        and atomically rename it to :meth:`local_path` on success.

        Parameters
        ----------
        job : DownloadJob
            Download coordinates.

        Returns
        -------
        Path
            Absolute path to the downloaded file.
        """

    # ------------------------------------------------------------------
    # Optional interface — override only when local_path() alone can't
    # disambiguate what's actually in the file (see content_key's own
    # docstring, and the module docstring above)
    # ------------------------------------------------------------------

    def content_key(self, job: DownloadJob) -> str | None:
        """Return a string identifying *what request produced this
        file's content*, or ``None`` (the default) if that's always
        implied by :meth:`local_path` alone.

        Parameters
        ----------
        job : DownloadJob
            Download coordinates.

        Returns
        -------
        str or None
            ``None`` (default): :meth:`is_complete`/:meth:`get` behave
            exactly as if this method didn't exist -- no sidecar file is
            read or written. Any other value: enables the sidecar
            content-key check in :meth:`is_complete` and the sidecar
            write in :meth:`get`.
        """
        return None

    # ------------------------------------------------------------------
    # Concrete shared logic — not normally overridden
    # ------------------------------------------------------------------

    def is_complete(self, job: DownloadJob) -> bool:
        """Return ``True`` if the local file exists and size is valid.

        A file is considered complete when:

        1. It exists at :meth:`local_path`.
        2. Its on-disk size is non-zero.
        3. Its size equals the remote size reported by
           :meth:`remote_size` — or the remote size is ``None``
           (cannot be checked), in which case existence alone is
           treated as sufficient.
        4. If :meth:`content_key` returns non-``None``: either no
           sidecar file exists yet (trust the local file, same as
           before this check existed -- e.g. every already-completed
           archive predating this feature), or the sidecar's recorded
           key matches :meth:`content_key` for *this* job. A mismatch
           means the existing file was downloaded for a different
           request (e.g. a different area/bbox) and is NOT complete
           for the current one -- forces a real re-fetch instead of
           silently reusing the wrong content.

        Parameters
        ----------
        job : DownloadJob
            Download coordinates.
        """
        path = self.local_path(job)
        if not path.exists():
            return False
        if path.stat().st_size == 0:
            return False
        expected = self.remote_size(job)
        if expected is not None and path.stat().st_size != expected:
            return False

        key = self.content_key(job)
        if key is not None:
            sidecar = _content_key_path(path)
            if sidecar.exists() and sidecar.read_text().strip() != key:
                return False
        return True

    def get(self, job: DownloadJob) -> Path:
        """Return the local file, downloading only if necessary.

        Applies the template-method check-then-fetch pattern:

        1. If :meth:`is_complete` is ``True``, return
           :meth:`local_path` immediately (no network I/O).
        2. Otherwise delegate to :meth:`_fetch`, then (if
           :meth:`content_key` returns non-``None``) atomically write a
           sidecar file recording the key used, so a future request for
           a *different* key against the same :meth:`local_path` is
           correctly detected as incomplete rather than silently reused.

        Parameters
        ----------
        job : DownloadJob
            Download coordinates.

        Returns
        -------
        Path
            Absolute path to the (possibly freshly downloaded) file.
        """
        if self.is_complete(job):
            return self.local_path(job)
        result = self._fetch(job)
        key = self.content_key(job)
        if key is not None:
            sidecar = _content_key_path(result)
            tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
            tmp.write_text(key)
            tmp.replace(sidecar)
        return result


def _content_key_path(local_path: Path) -> Path:
    """Sidecar path for *local_path*'s content key: ``<name>.area``."""
    return local_path.with_name(local_path.name + ".area")
