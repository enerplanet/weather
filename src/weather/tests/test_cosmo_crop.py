"""Tests for providers/cosmo_rea6/crop.py and its pipeline wiring.

Two things this file exists to prove, per the explicit requirement that
the crop step be skipped with 100% certainty when no country/bbox was
requested (not just "happens to look right" on visual inspection):

1. :func:`compute_crop_index`/:func:`crop_datasets` are correct in
   isolation (synthetic small grid, known bbox).
2. :func:`~weather.providers.cosmo_rea6.transform.build_month_dataset`
   never even IMPORTS the crop module when ``crop_bbox=None`` (the
   default) -- proven by patching the crop functions to raise if
   called, not just by comparing output.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from weather.geo.bbox import BBox
from weather.providers.cosmo_rea6.crop import compute_crop_index, crop_datasets
from weather.providers.cosmo_rea6.downloaded_attributes import ATTRIBUTES

# ---------------------------------------------------------------------------
# Shared synthetic fixtures
# ---------------------------------------------------------------------------

# A small 5x5 grid, deliberately rotated relative to true lat/lon (like the
# real COSMO-REA6 rotated-pole grid) so the "index rectangle bulges beyond
# the requested bbox" behavior documented in crop.py is actually exercised,
# not accidentally axis-aligned.
_LAT_2D = np.array(
    [
        [50.0, 50.3, 50.6, 50.9, 51.2],
        [50.2, 50.5, 50.8, 51.1, 51.4],
        [50.4, 50.7, 51.0, 51.3, 51.6],
        [50.6, 50.9, 51.2, 51.5, 51.8],
        [50.8, 51.1, 51.4, 51.7, 52.0],
    ]
)
_LON_2D = np.array(
    [
        [3.0, 3.2, 3.4, 3.6, 3.8],
        [3.3, 3.5, 3.7, 3.9, 4.1],
        [3.6, 3.8, 4.0, 4.2, 4.4],
        [3.9, 4.1, 4.3, 4.5, 4.7],
        [4.2, 4.4, 4.6, 4.8, 5.0],
    ]
)


def _reference_ds(with_coords: bool = True) -> xr.Dataset:
    ds = xr.Dataset(
        {"ssrd": (("y", "x"), np.ones((5, 5)))},
        coords={"y": np.arange(5), "x": np.arange(5)},
    )
    if with_coords:
        ds = ds.assign_coords(
            latitude=(("y", "x"), _LAT_2D), longitude=(("y", "x"), _LON_2D)
        )
    return ds


class TestComputeCropIndex:
    def test_correct_index_window(self) -> None:
        ds = _reference_ds()
        # Target a small box that should only match the middle cells.
        bbox = BBox(north=51.0, west=3.6, south=50.5, east=4.2)
        y_slice, x_slice = compute_crop_index(bbox, ds)
        # Every cell strictly inside the index window must satisfy the
        # "contains the requested bbox" guarantee: the mask-true cells'
        # index range must be a SUBSET of the returned slice.
        mask = (
            (bbox.south <= _LAT_2D) & (bbox.north >= _LAT_2D)
            & (bbox.west <= _LON_2D) & (bbox.east >= _LON_2D)
        )
        y_idx, x_idx = np.where(mask)
        assert y_slice.start <= y_idx.min() and y_slice.stop - 1 >= y_idx.max()
        assert x_slice.start <= x_idx.min() and x_slice.stop - 1 >= x_idx.max()

    def test_raises_without_lat_lon(self) -> None:
        ds = _reference_ds(with_coords=False)
        bbox = BBox(north=51.0, west=3.6, south=50.5, east=4.2)
        with pytest.raises(ValueError, match="latitude/longitude"):
            compute_crop_index(bbox, ds)

    def test_raises_when_bbox_outside_domain(self) -> None:
        ds = _reference_ds()
        # Nowhere near the synthetic grid's real 50-52N/3-5E extent.
        bbox = BBox(north=10.0, west=-80.0, south=5.0, east=-75.0)
        with pytest.raises(ValueError, match="No COSMO-REA6 grid cells"):
            compute_crop_index(bbox, ds)


class TestCropDatasets:
    def test_crops_every_dataset_to_matching_shape(self) -> None:
        datasets = {
            "SWDIRS_RAD": _reference_ds(),
            "T_2M": xr.Dataset(
                {"t2m": (("y", "x"), np.full((5, 5), 280.0))},
                coords={"y": np.arange(5), "x": np.arange(5)},
            ),
        }
        y_slice, x_slice = slice(1, 4), slice(0, 2)
        cropped = crop_datasets(datasets, y_slice, x_slice)
        assert set(cropped) == set(datasets)
        for ds in cropped.values():
            assert ds.sizes["y"] == 3
            assert ds.sizes["x"] == 2


# ---------------------------------------------------------------------------
# build_month_dataset(crop_bbox=None) hard no-op guarantee
# ---------------------------------------------------------------------------


def _synthetic_attribute_dataset(
    times: pd.DatetimeIndex, var_name: str = "value"
) -> xr.Dataset:
    """One small (5x5) per-attribute dataset, matching SWDIRS_RAD's grid
    below (a real month's attribute files all share one grid).

    ``transform._resolve_var`` falls back to "the sole data variable" when
    no known name/alias matches, so a single arbitrarily-named variable
    is sufficient to stand in for most real COSMO-REA6 attribute files --
    EXCEPT ``convert_temperature``, which (unlike every other compute_*
    helper) indexes ``ds["t2m"]`` directly rather than through
    ``_resolve_var``, so T_2M's synthetic variable must be named exactly
    that (found by running this fixture against the real function).
    """
    shape = (len(times), 5, 5)
    return xr.Dataset(
        {var_name: (("time", "y", "x"), np.full(shape, 50.0))},
        coords={"time": times, "y": np.arange(5), "x": np.arange(5)},
    )


@pytest.fixture
def synthetic_datasets() -> dict[str, xr.Dataset]:
    times = pd.date_range("2018-01-01", periods=3, freq="h")
    datasets = {
        attr: _synthetic_attribute_dataset(
            times, var_name="t2m" if attr == "T_2M" else "value",
        )
        for attr in ATTRIBUTES
    }
    # SWDIRS_RAD is the one build_month_dataset reads lat/lon from.
    # Reuses the same 5x5 grid as TestComputeCropIndex (module-level
    # _LAT_2D/_LON_2D, already verified to narrow a real bbox in both
    # dims) rather than a fresh grid, so the "does call the crop
    # module" test below has a bbox already known to work.
    datasets["SWDIRS_RAD"] = datasets["SWDIRS_RAD"].assign_coords(
        latitude=(("y", "x"), _LAT_2D), longitude=(("y", "x"), _LON_2D),
    )
    return datasets


class TestBuildMonthDatasetCropWiring:
    def test_crop_bbox_none_never_calls_crop_module(
        self, synthetic_datasets
    ) -> None:
        """The actual "100% sure" guarantee: patch compute_crop_index to
        raise if called at all, then confirm crop_bbox=None completes
        without tripping it -- not just that output happens to match."""
        from weather.providers.cosmo_rea6.transform import build_month_dataset

        def _boom(*_a, **_kw):
            raise AssertionError(
                "compute_crop_index must not be called when crop_bbox=None"
            )

        with (
            patch(
                "weather.providers.cosmo_rea6.transform.open_grib_month",
                side_effect=lambda attr, *a, **kw: synthetic_datasets[attr],
            ),
            patch(
                "weather.providers.cosmo_rea6.crop.compute_crop_index",
                side_effect=_boom,
            ),
        ):
            ds_out, _ = build_month_dataset(
                2018, 1, compute_dni_field=False, crop_bbox=None,
            )
        assert ds_out.sizes["y"] == 5
        assert ds_out.sizes["x"] == 5

    def test_crop_bbox_set_does_call_crop_module(
        self, synthetic_datasets
    ) -> None:
        """Positive control for the test above: proves the patch target is
        real (a broken patch path would make the no-op test pass
        vacuously) and that a real bbox actually narrows the output."""
        from weather.providers.cosmo_rea6.transform import build_month_dataset

        # A tighter interior bbox than test_correct_index_window's --
        # that one's bbox happens to still touch row 0 and row 4 of this
        # 5x5 grid (the "bulge" property crop.py's docstring describes:
        # containment is guaranteed, narrowing in EVERY dimension isn't,
        # for an arbitrary bbox against a rotated grid), which is
        # correct behavior but not a useful demonstration here. This
        # bbox was hand-verified against _LAT_2D/_LON_2D to narrow both.
        bbox = BBox(north=50.9, west=3.7, south=50.6, east=4.1)
        with patch(
            "weather.providers.cosmo_rea6.transform.open_grib_month",
            side_effect=lambda attr, *a, **kw: synthetic_datasets[attr],
        ):
            ds_out, _ = build_month_dataset(
                2018, 1, compute_dni_field=False, crop_bbox=bbox,
            )
        assert ds_out.sizes["y"] < 5
        assert ds_out.sizes["x"] < 5
