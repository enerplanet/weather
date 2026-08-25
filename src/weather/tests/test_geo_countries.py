"""Unit tests for :mod:`weather.geo`.

Covers the pure lookup/conversion logic (no ``cdo`` needed). One smoke
test exercises :func:`~weather.geo.crop.crop_netcdf` against a tiny
synthetic regular-grid NetCDF and is skipped if ``cdo`` isn't installed.

Run with::

    conda run -n weather_env pytest src/weather/tests/test_geo_countries.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from weather.geo.bbox import BBox
from weather.geo.countries import get_bbox, list_countries, normalize_country
from weather.geo.crop import crop_netcdf

# ---------------------------------------------------------------------------
# countries.py
# ---------------------------------------------------------------------------


def test_list_countries_sorted_and_nonempty():
    countries = list_countries()
    assert countries == sorted(countries)
    assert "netherlands" in countries
    assert "europe" not in countries  # implicit default domain, not a lookup


def test_normalize_country_lowercases_and_underscores():
    assert normalize_country("Czech Republic") == "czech_republic"
    assert normalize_country("  Germany ") == "germany"


def test_get_bbox_known_country():
    bbox = get_bbox("Netherlands")
    assert isinstance(bbox, BBox)
    # Netherlands: roughly 50.7-53.5 N, 3.3-7.2 E
    assert 50 < bbox.south < bbox.north < 54
    assert 3 < bbox.west < bbox.east < 8


def test_get_bbox_unknown_country_raises():
    with pytest.raises(ValueError, match="Unknown country"):
        get_bbox("narnia")


def test_uk_and_united_kingdom_are_independent_equal_entries():
    assert get_bbox("uk") == get_bbox("united_kingdom")


# ---------------------------------------------------------------------------
# bbox.py
# ---------------------------------------------------------------------------


def test_bbox_to_area_list_matches_n_w_s_e_order():
    bbox = BBox(north=53.5, west=3.3, south=50.7, east=7.2)
    assert bbox.to_area_list() == [53.5, 3.3, 50.7, 7.2]


def test_bbox_to_cdo_lonlatbox_matches_w_e_s_n_order():
    bbox = BBox(north=53.5, west=3.3, south=50.7, east=7.2)
    assert bbox.to_cdo_lonlatbox() == (3.3, 7.2, 50.7, 53.5)


def test_bbox_parse_round_trips_area_list():
    bbox = BBox.parse("53.472,3.358,50.751,7.21")
    assert bbox == BBox(north=53.472, west=3.358, south=50.751, east=7.21)


def test_bbox_parse_wrong_count_raises():
    with pytest.raises(ValueError, match="4 numbers"):
        BBox.parse("53.472,3.358,50.751")


def test_bbox_parse_non_numeric_raises():
    with pytest.raises(ValueError, match="numeric"):
        BBox.parse("north,west,south,east")


def test_bbox_parse_tolerates_whitespace():
    bbox = BBox.parse(" 53.472, 3.358 ,50.751,7.21 ")
    assert bbox == BBox(north=53.472, west=3.358, south=50.751, east=7.21)


# ---------------------------------------------------------------------------
# crop.py (requires the cdo binary)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("cdo"), reason="cdo binary not installed")
def test_crop_netcdf_regular_grid(tmp_path: Path):
    lat = np.linspace(40.0, 60.0, 21)   # 1 deg steps
    lon = np.linspace(0.0, 20.0, 21)
    data = np.random.rand(len(lat), len(lon)).astype("float32")
    ds = xr.Dataset(
        {"T": (("lat", "lon"), data)},
        coords={"lat": lat, "lon": lon},
    )
    # CDO only recognises this as a lonlat grid (not "generic") with CF
    # axis/units attrs on the coordinate variables -- real ERA5-Land/MERRA-2
    # exports carry these already, so set them explicitly here too.
    ds["lat"].attrs = {
        "units": "degrees_north", "standard_name": "latitude", "axis": "Y"
    }
    ds["lon"].attrs = {
        "units": "degrees_east", "standard_name": "longitude", "axis": "X"
    }
    input_path = tmp_path / "synthetic.nc"
    output_path = tmp_path / "synthetic_cropped.nc"
    ds.to_netcdf(input_path)

    bbox = BBox(north=53.5, west=3.3, south=50.7, east=7.2)
    crop_netcdf(input_path, output_path, bbox)

    cropped = xr.open_dataset(output_path)
    assert cropped["lat"].min() >= bbox.south - 1.0
    assert cropped["lat"].max() <= bbox.north + 1.0
    assert cropped["lon"].min() >= bbox.west - 1.0
    assert cropped["lon"].max() <= bbox.east + 1.0
    cropped.close()
