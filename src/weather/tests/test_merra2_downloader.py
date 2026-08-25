"""Tests for merra2/downloader.py's local_path()/content_key() region
awareness -- the direct regression test for the reported bug (a file
downloaded for one country silently reused for a different country's
request against the same year/month/day/collection).

No heavy deps: merra2/downloader.py doesn't import cfgrib/xarray, so
this file (unlike the ERA5-Land equivalent) needs no special PATH setup.
"""

from __future__ import annotations

from pathlib import Path

from weather.providers.merra2.downloader import Merra2Downloader, Merra2DownloadJob

_EUROPE = [72.0, -11.0, 34.0, 32.0]
_NETHERLANDS = [53.472, 3.358, 50.751, 7.21]
_GERMANY = [54.983, 5.988, 47.302, 15.016]


def _cfg(tmp_path: Path, area: list[float], region_tag: str | None = None) -> dict:
    return {"download_dir": tmp_path, "area": area, "region_tag": region_tag}


def _job() -> Merra2DownloadJob:
    return Merra2DownloadJob(collection="rad", year=2018, month=1, day=1)


class TestLocalPathUntagged:
    def test_matches_pre_existing_convention_exactly(self, tmp_path):
        """Regression pin: untagged filenames must stay byte-for-byte
        identical to what every already-downloaded real archive file
        (region_tag never existed before this feature) already has."""
        dl = Merra2Downloader(_cfg(tmp_path, _EUROPE, region_tag=None))
        path = dl.local_path(_job())
        assert path == tmp_path / "MERRA2_rad_20180101.nc4"

    def test_none_and_empty_string_both_untagged(self, tmp_path):
        for tag in (None, ""):
            dl = Merra2Downloader(_cfg(tmp_path, _EUROPE, region_tag=tag))
            assert dl.local_path(_job()).name == "MERRA2_rad_20180101.nc4"


class TestLocalPathTagged:
    def test_tag_inserted_after_collection(self, tmp_path):
        dl = Merra2Downloader(_cfg(tmp_path, _NETHERLANDS, region_tag="NL"))
        path = dl.local_path(_job())
        assert path == tmp_path / "MERRA2_rad_NL_20180101.nc4"

    def test_two_different_tags_produce_disjoint_paths(self, tmp_path):
        """The direct regression test for the reported bug."""
        nl = Merra2Downloader(_cfg(tmp_path, _NETHERLANDS, region_tag="NL"))
        de = Merra2Downloader(_cfg(tmp_path, _GERMANY, region_tag="DE"))
        job = _job()
        assert nl.local_path(job) != de.local_path(job)


class TestContentKey:
    def test_reflects_area(self, tmp_path):
        dl = Merra2Downloader(_cfg(tmp_path, _NETHERLANDS))
        assert dl.content_key(_job()) == "53.472,3.358,50.751,7.21"

    def test_different_areas_produce_different_keys(self, tmp_path):
        nl = Merra2Downloader(_cfg(tmp_path, _NETHERLANDS))
        de = Merra2Downloader(_cfg(tmp_path, _GERMANY))
        job = _job()
        assert nl.content_key(job) != de.content_key(job)

    def test_never_none_for_merra2(self, tmp_path):
        """Unlike ERA5-Land (global-by-default -> "GLOBAL"), MERRA-2's
        area always has at least the Europe default -- content_key()
        should never be None, so the sidecar safety net is always
        active for this provider."""
        dl = Merra2Downloader(_cfg(tmp_path, _EUROPE))
        assert dl.content_key(_job()) is not None
