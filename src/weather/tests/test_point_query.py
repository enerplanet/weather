"""Unit tests for the point-query path: point_query, dni_reconstruction,
geo_lookup.

These exercise ``weather.get_point_weather`` end-to-end against synthetic
NetCDFs shaped like each provider's real export (not real provider data),
since none of these three modules had any test coverage before this file
was added. Skipped automatically if xarray/netcdf4/pvlib aren't installed
(the ``pointquery``/``solar`` extras).

Run with::

    conda run -n weather_env pytest src/weather/tests/test_point_query.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("netCDF4")
pytest.importorskip("pvlib")

from weather import get_point_weather  # noqa: E402
from weather.common.dni_reconstruction import reconstruct_dni_dhi  # noqa: E402
from weather.common.geo_lookup import find_nearest_cell  # noqa: E402

REQUIRED_COLUMNS = ["T", "GHI", "DHI", "DNI"]


def _synthetic_ghi(times: pd.DatetimeIndex, shape: tuple[int, ...]) -> np.ndarray:
    """A simple periodic GHI-like signal, broadcast to *shape*.

    Not solar-position-accurate (real daylight hours will get masked to
    zero downstream) -- only meant to exercise the code path, not to be
    physically realistic.
    """
    base = np.clip(500 * np.sin(np.linspace(0, 8 * np.pi, len(times))), 0, None)
    return base.reshape((len(times),) + (1,) * (len(shape) - 1)) * np.ones(shape)


@pytest.fixture
def hourly_times() -> pd.DatetimeIndex:
    return pd.date_range("2018-06-01", periods=48, freq="h", tz=None)


class TestGeoLookup:
    def test_find_nearest_cell(self) -> None:
        lat_2d = np.array([[50.0, 50.1], [50.2, 50.3]])
        lon_2d = np.array([[4.0, 4.1], [4.2, 4.3]])
        ds = xr.Dataset(
            {"dummy": (("y", "x"), np.zeros((2, 2)))},
            coords={
                "latitude": (("y", "x"), lat_2d),
                "longitude": (("y", "x"), lon_2d),
            },
        )
        iy, ix = find_nearest_cell(ds, 50.08, 4.08)
        assert (iy, ix) == (0, 1)

    def test_find_nearest_cell_missing_coords_raises(self) -> None:
        ds = xr.Dataset({"dummy": (("y", "x"), np.zeros((2, 2)))})
        with pytest.raises(KeyError):
            find_nearest_cell(ds, 50.0, 4.0)


class TestDniReconstruction:
    def test_dirint_night_masked_and_non_negative(
        self, hourly_times: pd.DatetimeIndex
    ) -> None:
        ghi = pd.Series(_synthetic_ghi(hourly_times, (len(hourly_times),)), index=hourly_times)
        result = reconstruct_dni_dhi(ghi, latitude=52.0, longitude=5.0, method="dirint")
        assert set(result) == {"DNI", "DHI"}
        assert (result["DNI"] >= 0).all()
        assert (result["DHI"] >= 0).all()

    def test_disc_clip_to_extraterrestrial(self, hourly_times: pd.DatetimeIndex) -> None:
        ghi = pd.Series(_synthetic_ghi(hourly_times, (len(hourly_times),)), index=hourly_times)
        result = reconstruct_dni_dhi(
            ghi,
            latitude=52.0,
            longitude=5.0,
            method="disc",
            zenith_kind="apparent",
            clip_to_extraterrestrial=True,
            clip_dhi_to_ghi=True,
        )
        # Compare positionally, not by reindexing on the (tz-aware) result
        # index against the (tz-naive) input index -- that mismatch is
        # exactly the footgun _align_pressure/_strip_tz guard against
        # elsewhere in this codebase.
        assert (result["DHI"].to_numpy() <= ghi.to_numpy() + 1e-6).all()

    def test_unknown_method_raises(self, hourly_times: pd.DatetimeIndex) -> None:
        ghi = pd.Series(_synthetic_ghi(hourly_times, (len(hourly_times),)), index=hourly_times)
        with pytest.raises(ValueError):
            reconstruct_dni_dhi(ghi, latitude=52.0, longitude=5.0, method="bogus")


class TestGetPointWeatherRegularGrid:
    """ERA5-Land/MERRA-2-shaped archives: y/x dims, 1-D lat/lon aux coords."""

    def _write_archive(
        self, tmp_path, subdir, filename, hourly_times, pressure_var, with_wind=False
    ):
        lat_vals = np.array([50.0, 50.1, 50.2])
        lon_vals = np.array([4.0, 4.1, 4.2])
        shape = (len(hourly_times), 3, 3)
        ghi = _synthetic_ghi(hourly_times, shape)
        t = 15 + np.zeros(shape)
        pres = 101000 + np.zeros(shape)

        data_vars = {
            "GHI": (("time", "y", "x"), ghi),
            "T": (("time", "y", "x"), t),
            pressure_var: (("time", "y", "x"), pres),
        }
        if with_wind:
            data_vars["U_10M"] = (("time", "y", "x"), 3.0 + np.zeros(shape))
            data_vars["V_10M"] = (("time", "y", "x"), 2.0 + np.zeros(shape))
            data_vars["WS_10M"] = (("time", "y", "x"), np.hypot(3.0, 2.0) + np.zeros(shape))

        ds = xr.Dataset(
            data_vars,
            coords={
                "time": hourly_times,
                "y": np.arange(3),
                "x": np.arange(3),
                "latitude": ("y", lat_vals),
                "longitude": ("x", lon_vals),
            },
        )
        out_dir = tmp_path / subdir / "output"
        out_dir.mkdir(parents=True)
        ds.to_netcdf(out_dir / filename)
        return out_dir

    def test_era5_land_point_query(self, tmp_path, hourly_times) -> None:
        out_dir = self._write_archive(
            tmp_path, "era5_land", "ERA5_LAND_2018_06_all_attrs.nc", hourly_times, "sp"
        )
        df = get_point_weather(
            50.05, 4.05, 2018, provider="era5-land", data_dir=out_dir, use_case="solar"
        )
        assert list(df.columns) == REQUIRED_COLUMNS
        assert not df.isna().any().any()
        assert df.index.tz is None

    def test_merra2_point_query(self, tmp_path, hourly_times) -> None:
        out_dir = self._write_archive(
            tmp_path, "merra2", "MERRA2_2018_06_all_attrs.nc", hourly_times, "PS"
        )
        df = get_point_weather(
            50.05, 4.05, 2018, provider="merra2", data_dir=out_dir, use_case="solar"
        )
        assert list(df.columns) == REQUIRED_COLUMNS
        assert not df.isna().any().any()

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError):
            get_point_weather(50.0, 4.0, 2018, provider="not-a-provider")

    def test_missing_archive_raises_file_not_found(self, tmp_path) -> None:
        out_dir = tmp_path / "era5_land" / "output"
        out_dir.mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            get_point_weather(
                50.0, 4.0, 2018, provider="era5-land", data_dir=out_dir, use_case="solar"
            )

    def test_wind_use_case(self, tmp_path, hourly_times) -> None:
        out_dir = self._write_archive(
            tmp_path, "era5_land", "ERA5_LAND_2018_06_all_attrs.nc", hourly_times,
            "sp", with_wind=True,
        )
        df = get_point_weather(
            50.05, 4.05, 2018, provider="era5-land", data_dir=out_dir, use_case="wind"
        )
        assert list(df.columns) == ["WS_10M", "U_10M", "V_10M"]
        assert not df.isna().any().any()

    def test_variables_subset_and_order(self, tmp_path, hourly_times) -> None:
        out_dir = self._write_archive(
            tmp_path, "merra2", "MERRA2_2018_06_all_attrs.nc", hourly_times,
            "PS", with_wind=True,
        )
        df = get_point_weather(
            50.05, 4.05, 2018, provider="merra2", data_dir=out_dir,
            variables="WS_10M,T",
        )
        # Column order follows the request, not the archive's own order.
        assert list(df.columns) == ["WS_10M", "T"]

    def test_wind_variable_missing_from_archive_raises_keyerror(
        self, tmp_path, hourly_times
    ) -> None:
        out_dir = self._write_archive(
            tmp_path, "era5_land", "ERA5_LAND_2018_06_all_attrs.nc", hourly_times,
            "sp", with_wind=False,
        )
        with pytest.raises(KeyError, match="WS_10M"):
            get_point_weather(
                50.05, 4.05, 2018, provider="era5-land", data_dir=out_dir, use_case="wind"
            )

    def test_both_variables_and_use_case_raises(self, tmp_path, hourly_times) -> None:
        out_dir = self._write_archive(
            tmp_path, "era5_land", "ERA5_LAND_2018_06_all_attrs.nc", hourly_times, "sp"
        )
        with pytest.raises(ValueError, match="at most one"):
            get_point_weather(
                50.05, 4.05, 2018, provider="era5-land", data_dir=out_dir,
                variables="T", use_case="solar",
            )


class TestGetPointWeatherCosmo:
    """COSMO-shaped archive: y/x dims, 2-D lat/lon coords."""

    def test_cosmo_point_query(self, tmp_path, hourly_times) -> None:
        lat_2d = np.array(
            [[50.0, 50.1, 50.2], [50.05, 50.15, 50.25], [50.1, 50.2, 50.3]]
        )
        lon_2d = np.array(
            [[4.0, 4.05, 4.1], [4.1, 4.15, 4.2], [4.2, 4.25, 4.3]]
        )
        shape = (len(hourly_times), 3, 3)
        ghi = _synthetic_ghi(hourly_times, shape)
        t = 15 + np.zeros(shape)

        ds = xr.Dataset(
            {
                "T": (("time", "y", "x"), t),
                "GHI": (("time", "y", "x"), ghi),
            },
            coords={
                "time": hourly_times,
                "y": np.arange(3),
                "x": np.arange(3),
                "latitude": (("y", "x"), lat_2d),
                "longitude": (("y", "x"), lon_2d),
            },
        )
        out_dir = tmp_path / "cosmo_rea6" / "output"
        out_dir.mkdir(parents=True)
        ds.to_netcdf(out_dir / "COSMO_REA6_2018.nc")

        df = get_point_weather(
            50.05, 4.05, 2018, provider="cosmo-rea6", data_dir=out_dir, use_case="solar"
        )
        assert list(df.columns) == REQUIRED_COLUMNS
        assert not df.isna().any().any()

    def test_cosmo_archive_without_lat_lon_raises(self, tmp_path, hourly_times) -> None:
        """Regression check for the exact gap found in review: already-completed
        COSMO archives predating the lat/lon-retention fix have no latitude/
        longitude coords at all, and must fail loudly (not silently) here."""
        shape = (len(hourly_times), 3, 3)
        ds = xr.Dataset(
            {
                "T": (("time", "y", "x"), 15 + np.zeros(shape)),
                "GHI": (("time", "y", "x"), _synthetic_ghi(hourly_times, shape)),
            },
            coords={"time": hourly_times, "y": np.arange(3), "x": np.arange(3)},
        )
        out_dir = tmp_path / "cosmo_rea6" / "output"
        out_dir.mkdir(parents=True)
        ds.to_netcdf(out_dir / "COSMO_REA6_2018.nc")

        with pytest.raises(KeyError):
            get_point_weather(
                50.05, 4.05, 2018, provider="cosmo-rea6", data_dir=out_dir, use_case="solar"
            )

    def test_cosmo_wind_use_case(self, tmp_path, hourly_times) -> None:
        lat_2d = np.array(
            [[50.0, 50.1, 50.2], [50.05, 50.15, 50.25], [50.1, 50.2, 50.3]]
        )
        lon_2d = np.array(
            [[4.0, 4.05, 4.1], [4.1, 4.15, 4.2], [4.2, 4.25, 4.3]]
        )
        shape = (len(hourly_times), 3, 3)
        ds = xr.Dataset(
            {
                "T": (("time", "y", "x"), 15 + np.zeros(shape)),
                "GHI": (("time", "y", "x"), _synthetic_ghi(hourly_times, shape)),
                "U_10M": (("time", "y", "x"), 3.0 + np.zeros(shape)),
                "V_10M": (("time", "y", "x"), 2.0 + np.zeros(shape)),
                "WS_10M": (("time", "y", "x"), np.hypot(3.0, 2.0) + np.zeros(shape)),
            },
            coords={
                "time": hourly_times,
                "y": np.arange(3),
                "x": np.arange(3),
                "latitude": (("y", "x"), lat_2d),
                "longitude": (("y", "x"), lon_2d),
            },
        )
        out_dir = tmp_path / "cosmo_rea6" / "output"
        out_dir.mkdir(parents=True)
        ds.to_netcdf(out_dir / "COSMO_REA6_2018.nc")

        df = get_point_weather(
            50.05, 4.05, 2018, provider="cosmo-rea6", data_dir=out_dir, use_case="wind"
        )
        assert list(df.columns) == ["WS_10M", "U_10M", "V_10M"]
        assert not df.isna().any().any()
