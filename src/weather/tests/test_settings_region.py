"""Unit tests for WEATHER_REGION-driven work-dir/area scoping.

Covers ``EnvSettings.era5_work_dir()``/``merra2_work_dir()``/
``cosmo_work_dir()``/``merra2_area()`` and
``era5_land.config._area_from_env()``. No filesystem or network access;
every test isolates ``WEATHER_DATA_DIR`` to ``tmp_path`` and clears
``WEATHER_REGION``/``*_WORK_DIR``/``*_AREA`` so the real repo's ``.env``
(which sets ``ERA5_AREA``/``MERRA2_AREA`` explicitly) can't mask the
unset-value code paths under test.

Run with::

    conda run -n weather_env pytest src/weather/tests/test_settings_region.py
"""

from __future__ import annotations

import pytest

from weather.geo.countries import get_bbox
from weather.providers.era5_land.config import _area_from_env
from weather.settings import EnvSettings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_DATA_DIR", str(tmp_path))
    for name in (
        "WEATHER_REGION",
        "ERA5_WORK_DIR",
        "MERRA_WORK_DIR",
        "COSMO_WORK_DIR",
        "ERA5_AREA",
        "MERRA2_AREA",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# work-dir scoping
# ---------------------------------------------------------------------------


def test_work_dir_flat_when_region_unset(tmp_path):
    assert EnvSettings.era5_work_dir() == (tmp_path / "era5_land").resolve()
    assert EnvSettings.merra2_work_dir() == (tmp_path / "merra2").resolve()


def test_work_dir_region_scoped_when_region_set(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_REGION", "germany")
    assert EnvSettings.era5_work_dir() == (tmp_path / "era5_land" / "germany").resolve()
    assert EnvSettings.merra2_work_dir() == (tmp_path / "merra2" / "germany").resolve()


def test_explicit_work_dir_wins_over_region(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_REGION", "germany")
    monkeypatch.setenv("ERA5_WORK_DIR", str(tmp_path / "explicit"))
    assert EnvSettings.era5_work_dir() == (tmp_path / "explicit").resolve()


def test_cosmo_work_dir_unaffected_by_region(monkeypatch, tmp_path):
    """Regression guard: COSMO has no AREA parameter, so it must stay flat."""
    monkeypatch.setenv("WEATHER_REGION", "germany")
    assert EnvSettings.cosmo_work_dir() == (tmp_path / "cosmo_rea6").resolve()


# ---------------------------------------------------------------------------
# area derivation
# ---------------------------------------------------------------------------


def test_area_derived_from_region_when_unset(monkeypatch):
    monkeypatch.setenv("WEATHER_REGION", "germany")
    expected = get_bbox("germany").to_area_list()
    assert _area_from_env() == expected
    assert EnvSettings.merra2_area() == expected


def test_explicit_area_wins_over_region(monkeypatch):
    monkeypatch.setenv("WEATHER_REGION", "germany")
    monkeypatch.setenv("ERA5_AREA", "1,2,3,4")
    monkeypatch.setenv("MERRA2_AREA", "1,2,3,4")
    assert _area_from_env() == [1.0, 2.0, 3.0, 4.0]
    assert EnvSettings.merra2_area() == [1.0, 2.0, 3.0, 4.0]


def test_area_raises_when_neither_area_nor_region_set():
    with pytest.raises(ValueError, match="ERA5_AREA is required"):
        _area_from_env()
    with pytest.raises(ValueError, match="MERRA2_AREA is required"):
        EnvSettings.merra2_area()


# ---------------------------------------------------------------------------
# unknown region
# ---------------------------------------------------------------------------


def test_unknown_region_raises_on_work_dir(monkeypatch):
    monkeypatch.setenv("WEATHER_REGION", "narnia")
    with pytest.raises(ValueError, match="Unknown country"):
        EnvSettings.era5_work_dir()


def test_unknown_region_raises_on_area(monkeypatch):
    monkeypatch.setenv("WEATHER_REGION", "narnia")
    with pytest.raises(ValueError, match="Unknown country"):
        _area_from_env()
