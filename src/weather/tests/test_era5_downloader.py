"""Tests for era5_land/downloader.py's local_path()/content_key() region
awareness -- the direct regression test for the reported bug (a file
downloaded for one country silently reused for a different country's
request against the same year/month).

Note: importing weather.providers.era5_land (any submodule) runs that
package's __init__.py, which pulls in transform.py -> cfgrib -> eccodes.
On Windows dev this needs <env>/Library/bin on PATH (a known, cosmetic,
non-CI issue -- see CLAUDE.md); CI runs on Linux where this is a
non-issue via normal conda activation.
"""

from __future__ import annotations

from pathlib import Path

from weather.providers.era5_land.downloaded_attributes import ATTRIBUTES
from weather.providers.era5_land.downloader import Era5Downloader

_ATTRS = list(ATTRIBUTES.keys())
_GLOBAL = None
_NETHERLANDS = [53.472, 3.358, 50.751, 7.21]
_GERMANY = [54.983, 5.988, 47.302, 15.016]


def _cfg(
    tmp_path: Path, area, region_tag: str | None = None, data_format: str = "grib",
) -> dict:
    return {
        "download_dir": tmp_path,
        "area": area,
        "region_tag": region_tag,
        "attributes": _ATTRS,
        "data_format": data_format,
    }


def _job():
    from weather.providers.base_downloader import DownloadJob

    return DownloadJob(attribute="all_attrs", year=2018, month=1)


class TestLocalPathUntagged:
    def test_matches_pre_existing_convention_exactly(self, tmp_path):
        """Regression pin: untagged filenames must stay byte-for-byte
        identical to what every already-downloaded real archive file
        (region_tag never existed before this feature) already has."""
        dl = Era5Downloader(_cfg(tmp_path, _GLOBAL, region_tag=None))
        path = dl.local_path(_job())
        assert path == tmp_path / "ERA5_LAND_2018_01_all_attrs.grib"

    def test_none_and_empty_string_both_untagged(self, tmp_path):
        for tag in (None, ""):
            dl = Era5Downloader(_cfg(tmp_path, _GLOBAL, region_tag=tag))
            assert dl.local_path(_job()).name == "ERA5_LAND_2018_01_all_attrs.grib"

    def test_nc_extension_respected(self, tmp_path):
        dl = Era5Downloader(_cfg(tmp_path, _GLOBAL, data_format="netcdf"))
        assert dl.local_path(_job()).suffix == ".nc"


class TestLocalPathTagged:
    def test_tag_inserted_after_prefix(self, tmp_path):
        dl = Era5Downloader(_cfg(tmp_path, _NETHERLANDS, region_tag="NL"))
        path = dl.local_path(_job())
        assert path == tmp_path / "ERA5_LAND_NL_2018_01_all_attrs.grib"

    def test_two_different_tags_produce_disjoint_paths(self, tmp_path):
        """The direct regression test for the reported bug."""
        nl = Era5Downloader(_cfg(tmp_path, _NETHERLANDS, region_tag="NL"))
        de = Era5Downloader(_cfg(tmp_path, _GERMANY, region_tag="DE"))
        job = _job()
        assert nl.local_path(job) != de.local_path(job)


class TestContentKey:
    def test_global_when_area_unset(self, tmp_path):
        dl = Era5Downloader(_cfg(tmp_path, _GLOBAL))
        assert dl.content_key(_job()) == "GLOBAL"

    def test_reflects_area_when_set(self, tmp_path):
        dl = Era5Downloader(_cfg(tmp_path, _NETHERLANDS))
        assert dl.content_key(_job()) == "53.472,3.358,50.751,7.21"

    def test_different_areas_produce_different_keys(self, tmp_path):
        nl = Era5Downloader(_cfg(tmp_path, _NETHERLANDS))
        de = Era5Downloader(_cfg(tmp_path, _GERMANY))
        job = _job()
        assert nl.content_key(job) != de.content_key(job)
