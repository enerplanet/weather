"""Tests for base_downloader.py's content_key()/sidecar mechanism.

Two things this file exists to prove, mirroring the "100% sure" bar
already established for providers/cosmo_rea6/crop.py's crop_bbox hook
(see test_cosmo_crop.py):

1. The default `content_key() -> None` is a hard no-op (COSMO's actual
   behavior): the sidecar mechanism is never consulted at all, not just
   "happens to look right" -- proven by planting an obviously-mismatched
   sidecar and confirming is_complete() still trusts the file.
2. When content_key() IS overridden (ERA5-Land/MERRA-2's actual
   behavior), the sidecar correctly (a) trusts an existing file with no
   sidecar at all -- the zero-migration-risk guarantee for every
   already-completed real archive -- and (b) forces a real re-fetch on
   a genuine mismatch, which is the actual bug fix: a file downloaded
   for one area must never be silently reused for a different area's
   request.
"""

from __future__ import annotations

from pathlib import Path

from weather.providers.base_downloader import BaseDownloader, DownloadJob


class _FakeDownloader(BaseDownloader):
    """Minimal concrete subclass -- no real provider's network/GRIB
    dependencies, just enough to exercise is_complete()/get()."""

    def __init__(
        self,
        work_dir: Path,
        key: str | None = None,
        remote_size_value: int | None = None,
    ) -> None:
        self._work_dir = work_dir
        self._key = key
        self._remote_size_value = remote_size_value
        self.fetch_calls: list[DownloadJob] = []

    def remote_size(self, job: DownloadJob) -> int | None:
        return self._remote_size_value

    def local_path(self, job: DownloadJob) -> Path:
        return self._work_dir / f"{job.attribute}_{job.year}_{job.month:02d}.txt"

    def content_key(self, job: DownloadJob) -> str | None:
        return self._key

    def _fetch(self, job: DownloadJob) -> Path:
        self.fetch_calls.append(job)
        path = self.local_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data")
        return path


def _job() -> DownloadJob:
    return DownloadJob(attribute="T", year=2018, month=1)


def _sidecar_for(path: Path) -> Path:
    return path.with_name(path.name + ".area")


class TestContentKeyDefaultIsHardNoOp:
    """content_key() -> None (COSMO's actual, only behavior)."""

    def test_no_sidecar_written_on_fetch(self, tmp_path):
        dl = _FakeDownloader(tmp_path, key=None)
        job = _job()
        result = dl.get(job)
        assert result == dl.local_path(job)
        assert len(dl.fetch_calls) == 1
        assert not _sidecar_for(result).exists()

    def test_stray_sidecar_is_never_consulted(self, tmp_path):
        """Plant an obviously-mismatched sidecar next to an existing
        file; content_key()=None must mean is_complete() never looks
        at it at all -- proven, not assumed, by the reuse still
        happening despite the mismatch."""
        dl = _FakeDownloader(tmp_path, key=None)
        job = _job()
        path = dl.local_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data")
        _sidecar_for(path).write_text("some-other-key-that-would-mismatch")
        assert dl.is_complete(job) is True


class TestSidecarSafetyNet:
    """content_key() returns a real key (ERA5-Land/MERRA-2's behavior)."""

    def test_missing_sidecar_trusts_existing_file(self, tmp_path):
        """Zero-migration-risk guarantee: a pre-existing file with no
        sidecar (every already-completed real archive, today) is
        trusted exactly as before this feature existed."""
        dl = _FakeDownloader(tmp_path, key="52.0,4.0,50.0,6.0")
        job = _job()
        path = dl.local_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pre-existing data")
        assert dl.is_complete(job) is True
        result = dl.get(job)
        assert len(dl.fetch_calls) == 0
        assert result == path

    def test_matching_sidecar_avoids_refetch(self, tmp_path):
        dl = _FakeDownloader(tmp_path, key="NL-KEY")
        job = _job()
        dl.get(job)
        assert len(dl.fetch_calls) == 1
        dl.get(job)
        assert len(dl.fetch_calls) == 1

    def test_mismatched_sidecar_forces_refetch(self, tmp_path):
        """The actual bug fix: a file downloaded for one area must not
        be silently reused for a different area's request against the
        same local_path()."""
        dl_nl = _FakeDownloader(tmp_path, key="NL-KEY")
        job = _job()
        dl_nl.get(job)
        assert len(dl_nl.fetch_calls) == 1

        dl_de = _FakeDownloader(tmp_path, key="DE-KEY")
        assert dl_de.is_complete(job) is False
        dl_de.get(job)
        assert len(dl_de.fetch_calls) == 1

    def test_get_writes_sidecar_atomically_no_leftover_tmp(self, tmp_path):
        dl = _FakeDownloader(tmp_path, key="NL-KEY")
        result = dl.get(_job())
        sidecar = _sidecar_for(result)
        assert sidecar.exists()
        assert sidecar.read_text() == "NL-KEY"
        assert not sidecar.with_name(sidecar.name + ".tmp").exists()

    def test_size_mismatch_still_checked_before_sidecar(self, tmp_path):
        """Confirm the pre-existing remote-size check still works
        unchanged alongside the new sidecar check."""
        dl = _FakeDownloader(tmp_path, key="NL-KEY", remote_size_value=999)
        job = _job()
        path = dl.local_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data")  # size != 999
        assert dl.is_complete(job) is False
