"""Unit tests for ``weather fetch`` (:mod:`weather.unified_cli`).

Mocks at two seams so these stay fast and don't need cfgrib/eccodes or
real network/disk access:

1. ``weather.registry.get_provider`` -> a tiny fake with just a ``.name``
   attribute (the real registry lazily imports the full provider package,
   which pulls in cfgrib for cosmo-rea6/era5-land).
2. The per-provider ``pipeline`` module -> injected into ``sys.modules``
   under its real dotted path, so ``importlib.import_module()`` inside
   ``cmd_fetch`` finds the fake without ever touching the real module.

Run with::

    conda run -n weather_env pytest src/weather/tests/test_unified_cli.py
"""

from __future__ import annotations

import argparse
import os
import sys
import types
from pathlib import Path

import pytest

from weather import unified_cli


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "provider": "merra-2",
        "range": "single-month",
        "year": None,
        "month": None,
        "from_year": None,
        "to_year": None,
        "ncores": None,
        "work_dir": None,
        "complevel": 1,
        "skip_download": False,
        "skip_decompress": False,
        "skip_dni": False,
        "night_mask": False,
        "resume": False,
        "cleanup": None,
        "concatenate": "none",
        "output": None,
        "percentile": False,
        "which_percentile": None,
        "country": None,
        "bbox": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(
        "weather.registry.get_provider", lambda _arg: _FakeProvider(name)
    )


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch, canonical: str, run_pipeline
) -> list[dict]:
    """Inject a fake pipeline module; returns a list that captures each
    call's kwargs (appended to, one dict per call)."""
    calls: list[dict] = []

    def _wrapped(**kwargs):
        calls.append(kwargs)
        return run_pipeline(**kwargs)

    module_path = unified_cli._PIPELINE_MODULE[canonical]
    fake_mod = types.ModuleType(module_path)
    fake_mod.run_pipeline = _wrapped  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_path, fake_mod)
    return calls


def _monthly_paths(
    tmp_path: Path, canonical: str, year: int, months: list[int]
) -> list[Path]:
    prefix = unified_cli._FILE_PREFIX[canonical]
    out_dir = tmp_path / "output"
    out_dir.mkdir(exist_ok=True)
    paths = []
    for m in months:
        p = out_dir / f"{prefix}_{year}_{m:02d}_all_attrs.nc"
        p.touch()
        paths.append(p)
    return paths


def _patch_merge(monkeypatch: pytest.MonkeyPatch, on_call) -> None:
    """Patch NetCDFMerger.merge to record each call via *on_call*."""

    def _fake_merge(cls, monthly_paths, output_path, logger=None):
        on_call(monthly_paths, Path(output_path))

    monkeypatch.setattr(
        "weather.common.merge.NetCDFMerger.merge", classmethod(_fake_merge)
    )


# ---------------------------------------------------------------------------
# Validation / error paths -- return before any pipeline import happens
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_provider_reports_error(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            "weather.registry.get_provider",
            lambda _arg: (_ for _ in ()).throw(
                ValueError("Unknown provider 'x'")
            ),
        )
        rc = unified_cli.cmd_fetch(_args(provider="x"))
        assert rc == 1
        assert "Unknown provider" in capsys.readouterr().out

    def test_single_month_requires_year_and_month(
        self, monkeypatch, capsys
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        rc = unified_cli.cmd_fetch(_args(range="single-month", year=2018))
        assert rc == 1
        assert "requires --year and --month" in capsys.readouterr().out

    def test_single_month_rejects_concatenate(self, monkeypatch, capsys) -> None:
        _patch_provider(monkeypatch, "merra-2")
        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=3, concatenate="all")
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "not valid for --range single-month" in out

    def test_multi_year_per_year_rejects_output(self, monkeypatch, capsys) -> None:
        _patch_provider(monkeypatch, "merra-2")
        monkeypatch.setattr(unified_cli, "_settings_value", lambda *_: 2020)
        rc = unified_cli.cmd_fetch(
            _args(
                range="multi-year",
                from_year=2018,
                to_year=2019,
                concatenate="per-year",
                output="x.nc",
            )
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "not valid with --concatenate per-year" in out

    @pytest.mark.parametrize(
        ("flag", "provider", "message"),
        [
            ("skip_decompress", "era5-land", "cosmo-rea6 only"),
            ("skip_dni", "merra-2", "cosmo-rea6 only"),
            ("night_mask", "merra-2", "era5-land only"),
        ],
    )
    def test_provider_only_flags_rejected_for_other_providers(
        self, monkeypatch, capsys, flag, provider, message
    ) -> None:
        _patch_provider(monkeypatch, provider)
        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=1, **{flag: True})
        )
        assert rc == 1
        assert message in capsys.readouterr().out

    def test_country_accepted_for_cosmo_sets_crop_bbox(
        self, monkeypatch, capsys, tmp_path
    ) -> None:
        """cosmo-rea6 now supports --country/--bbox too (real end-to-end
        confirmed against local data -- see providers/cosmo_rea6/crop.py)
        via a local post-decompress crop, not the era5-land/merra-2
        env-var mechanism (DWD has no server-side area subsetting)."""
        _patch_provider(monkeypatch, "cosmo-rea6")
        calls = _install_fake_pipeline(
            monkeypatch,
            "cosmo-rea6",
            lambda **kw: _monthly_paths(tmp_path, "cosmo-rea6", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="cosmo-rea6", range="single-month", year=2018,
                month=1, country="netherlands",
            )
        )
        assert rc == 0
        assert len(calls) == 1
        crop_bbox = calls[0]["crop_bbox"]
        assert crop_bbox is not None
        assert crop_bbox.north == pytest.approx(53.472)
        assert "Crop bbox:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Dispatch -- fake pipeline module, real _concatenate/_run_percentile logic
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_single_month_calls_run_pipeline_with_month_list(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        calls = _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [3]),
        )
        rc = unified_cli.cmd_fetch(_args(range="single-month", year=2018, month=3))
        assert rc == 0
        assert len(calls) == 1
        assert calls[0]["year"] == 2018
        assert calls[0]["months"] == [3]
        # MERRA-2 has no skip_decompress/skip_dni/night_mask kwargs.
        assert "skip_decompress" not in calls[0]
        assert "night_mask" not in calls[0]

    def test_cosmo_kwargs_include_skip_decompress_and_skip_dni(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "cosmo-rea6")
        calls = _install_fake_pipeline(
            monkeypatch,
            "cosmo-rea6",
            lambda **kw: _monthly_paths(tmp_path, "cosmo-rea6", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="cosmo-rea6",
                range="single-month",
                year=2018,
                month=1,
                skip_decompress=True,
                skip_dni=True,
            )
        )
        assert rc == 0
        assert calls[0]["skip_decompress"] is True
        assert calls[0]["skip_dni"] is True

    def test_era5_kwargs_include_night_mask(self, monkeypatch, tmp_path) -> None:
        _patch_provider(monkeypatch, "era5-land")
        calls = _install_fake_pipeline(
            monkeypatch,
            "era5-land",
            lambda **kw: _monthly_paths(tmp_path, "era5-land", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="era5-land",
                range="single-month",
                year=2018,
                month=1,
                night_mask=True,
            )
        )
        assert rc == 0
        assert calls[0]["night_mask"] is True

    def test_single_year_defaults_year_from_settings(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        monkeypatch.setattr(unified_cli, "_settings_value", lambda *_: 2018)
        calls = _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(
                tmp_path, "merra-2", 2018, list(range(1, 13))
            ),
        )
        rc = unified_cli.cmd_fetch(_args(range="single-year", year=None))
        assert rc == 0
        assert calls[0]["year"] == 2018

    def test_single_year_concatenate_all_merges_with_canonical_name(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        monthly = _monthly_paths(tmp_path, "merra-2", 2023, list(range(1, 13)))
        _install_fake_pipeline(monkeypatch, "merra-2", lambda **kw: monthly)

        merge_calls = []
        _patch_merge(
            monkeypatch, lambda paths, dest: merge_calls.append((paths, dest))
        )

        rc = unified_cli.cmd_fetch(
            _args(range="single-year", year=2023, concatenate="all")
        )
        assert rc == 0
        assert len(merge_calls) == 1
        paths, dest = merge_calls[0]
        assert len(paths) == 12
        assert dest.name == "MERRA2_2023_annual_all_attrs.nc"

    def test_output_override_wins_over_canonical_name(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        monthly = _monthly_paths(tmp_path, "merra-2", 2023, [1])
        _install_fake_pipeline(monkeypatch, "merra-2", lambda **kw: monthly)

        merge_calls = []
        _patch_merge(monkeypatch, lambda paths, dest: merge_calls.append(dest))

        custom = str(tmp_path / "custom.nc")
        rc = unified_cli.cmd_fetch(
            _args(range="single-year", year=2023, concatenate="all", output=custom)
        )
        assert rc == 0
        assert merge_calls[0] == Path(custom)

    def test_multi_year_per_year_merges_once_per_year(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        monkeypatch.setattr(unified_cli, "_settings_value", lambda *_: 2018)

        def _fake_run(**kw):
            return _monthly_paths(tmp_path, "merra-2", kw["year"], [1, 2])

        _install_fake_pipeline(monkeypatch, "merra-2", _fake_run)

        merge_calls = []
        _patch_merge(
            monkeypatch, lambda paths, dest: merge_calls.append(dest.name)
        )
        rc = unified_cli.cmd_fetch(
            _args(
                range="multi-year",
                from_year=2018,
                to_year=2020,
                concatenate="per-year",
            )
        )
        assert rc == 0
        assert sorted(merge_calls) == [
            "MERRA2_2018_annual_all_attrs.nc",
            "MERRA2_2019_annual_all_attrs.nc",
            "MERRA2_2020_annual_all_attrs.nc",
        ]

    def test_multi_year_all_merges_once_across_range(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")

        def _fake_run(**kw):
            return _monthly_paths(tmp_path, "merra-2", kw["year"], [1])

        _install_fake_pipeline(monkeypatch, "merra-2", _fake_run)

        merge_calls = []
        _patch_merge(
            monkeypatch,
            lambda paths, dest: merge_calls.append((len(paths), dest.name)),
        )
        rc = unified_cli.cmd_fetch(
            _args(range="multi-year", from_year=2018, to_year=2020, concatenate="all")
        )
        assert rc == 0
        assert len(merge_calls) == 1
        count, name = merge_calls[0]
        assert count == 3  # one file per year, 3 years
        assert name == "MERRA2_2018_2020_all_attrs.nc"


# ---------------------------------------------------------------------------
# --country / --bbox area override
# ---------------------------------------------------------------------------


class TestAreaOverride:
    def test_country_sets_merra2_area_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("MERRA2_AREA", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=1, country="netherlands")
        )
        assert rc == 0
        assert os.environ["MERRA2_AREA"] == "53.472,3.358,50.751,7.21"

    def test_bbox_sets_era5_area_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("ERA5_AREA", raising=False)
        _patch_provider(monkeypatch, "era5-land")
        _install_fake_pipeline(
            monkeypatch,
            "era5-land",
            lambda **kw: _monthly_paths(tmp_path, "era5-land", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="era5-land",
                range="single-month",
                year=2018,
                month=1,
                bbox="52.5,4.5,51.5,5.5",
            )
        )
        assert rc == 0
        assert os.environ["ERA5_AREA"] == "52.5,4.5,51.5,5.5"

    def test_invalid_bbox_reports_error(self, monkeypatch, capsys) -> None:
        _patch_provider(monkeypatch, "era5-land")
        rc = unified_cli.cmd_fetch(
            _args(
                provider="era5-land",
                range="single-month",
                year=2018,
                month=1,
                bbox="not,a,bbox",
            )
        )
        assert rc == 1
        assert "Error" in capsys.readouterr().out

    def test_country_sets_merra2_region_tag_env_var(
        self, monkeypatch, tmp_path
    ) -> None:
        """The actual bug fix: without this, a second country-scoped
        fetch against the same work_dir can silently reuse the first
        country's cached raw download for the same year/month."""
        monkeypatch.delenv("MERRA2_AREA", raising=False)
        monkeypatch.delenv("MERRA2_REGION_TAG", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=1, country="netherlands")
        )
        assert rc == 0
        assert os.environ["MERRA2_REGION_TAG"] == "NL"

    def test_bbox_sets_era5_region_tag_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("ERA5_AREA", raising=False)
        monkeypatch.delenv("ERA5_REGION_TAG", raising=False)
        _patch_provider(monkeypatch, "era5-land")
        _install_fake_pipeline(
            monkeypatch,
            "era5-land",
            lambda **kw: _monthly_paths(tmp_path, "era5-land", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="era5-land",
                range="single-month",
                year=2018,
                month=1,
                bbox="52.5,4.5,51.5,5.5",
            )
        )
        assert rc == 0
        tag = os.environ["ERA5_REGION_TAG"]
        assert tag.startswith("CUSTOM-")
        assert tag != "CUSTOM"  # the pre-fix literal, must be disambiguated

    def test_two_different_bboxes_produce_different_custom_tags(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "era5-land")
        _install_fake_pipeline(
            monkeypatch,
            "era5-land",
            lambda **kw: _monthly_paths(tmp_path, "era5-land", 2018, [1]),
        )
        unified_cli.cmd_fetch(
            _args(
                provider="era5-land", range="single-month", year=2018, month=1,
                bbox="52.5,4.5,51.5,5.5",
            )
        )
        tag_a = os.environ["ERA5_REGION_TAG"]
        unified_cli.cmd_fetch(
            _args(
                provider="era5-land", range="single-month", year=2018, month=1,
                bbox="48.0,12.0,47.0,13.0",
            )
        )
        tag_b = os.environ["ERA5_REGION_TAG"]
        assert tag_a != tag_b

    def test_plain_fetch_never_sets_region_tag_env_vars(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("ERA5_REGION_TAG", raising=False)
        monkeypatch.delenv("MERRA2_REGION_TAG", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=1)
        )
        assert rc == 0
        assert "ERA5_REGION_TAG" not in os.environ
        assert "MERRA2_REGION_TAG" not in os.environ


# ---------------------------------------------------------------------------
# --percentile
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_percentile_instantiates_indexer_with_resolved_ncores(
        self, monkeypatch, tmp_path
    ) -> None:
        _patch_provider(monkeypatch, "merra-2")
        _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [1]),
        )

        def _fake_settings(_canonical: str, suffix: str):
            return str(tmp_path / "output") if suffix == "output_dir" else 8

        monkeypatch.setattr(unified_cli, "_settings_value", _fake_settings)

        captured: dict[str, object] = {}

        class _FakeIndexer:
            def __init__(self, *, source_dir, target_dir, n_cpu_cores):
                captured["source_dir"] = source_dir
                captured["target_dir"] = target_dir
                captured["n_cpu_cores"] = n_cpu_cores

            def execute_indexing_pipeline(self):
                captured["ran"] = True

        fake_mod = types.ModuleType("weather.providers.merra2.percentile_index")
        fake_mod.Merra2PercentileIndexer = _FakeIndexer  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules, "weather.providers.merra2.percentile_index", fake_mod
        )

        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=1, percentile=True)
        )
        assert rc == 0
        assert captured["ran"] is True
        assert captured["n_cpu_cores"] == 8
        assert captured["target_dir"] == str(tmp_path / "output" / "percentile")


# ---------------------------------------------------------------------------
# Region-tag filename insertion (--country / --bbox)
# ---------------------------------------------------------------------------


class TestRegionTag:
    def test_country_tag_renames_monthly_output(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("MERRA2_AREA", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [3]),
        )
        rc = unified_cli.cmd_fetch(
            _args(range="single-month", year=2018, month=3, country="netherlands")
        )
        assert rc == 0
        renamed = tmp_path / "output" / "MERRA2_NL_2018_03_all_attrs.nc"
        original = tmp_path / "output" / "MERRA2_2018_03_all_attrs.nc"
        assert renamed.exists()
        assert not original.exists()

    def test_bbox_tag_uses_custom(self, monkeypatch, tmp_path) -> None:
        """The tag is CUSTOM-<hash>, not the bare literal "CUSTOM" --
        two different --bbox values must not collide on the same
        tagged output filename (or, before this session's fix, the
        same raw download filename either)."""
        monkeypatch.delenv("ERA5_AREA", raising=False)
        _patch_provider(monkeypatch, "era5-land")
        _install_fake_pipeline(
            monkeypatch,
            "era5-land",
            lambda **kw: _monthly_paths(tmp_path, "era5-land", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="era5-land",
                range="single-month",
                year=2018,
                month=1,
                bbox="52.5,4.5,51.5,5.5",
            )
        )
        assert rc == 0
        matches = list((tmp_path / "output").glob("ERA5_LAND_CUSTOM-*_2018_01_all_attrs.nc"))
        assert len(matches) == 1

    def test_country_tag_renames_cosmo_output_too(
        self, monkeypatch, tmp_path
    ) -> None:
        """cosmo-rea6's tag-rename uses the exact same generic
        _tag_path()/_apply_region_tag() as era5-land/merra-2 above (only
        how the area gets INTO the pipeline call differs -- crop_bbox
        kwarg vs. an env var, see TestValidation's
        test_country_accepted_for_cosmo_sets_crop_bbox)."""
        _patch_provider(monkeypatch, "cosmo-rea6")
        _install_fake_pipeline(
            monkeypatch,
            "cosmo-rea6",
            lambda **kw: _monthly_paths(tmp_path, "cosmo-rea6", 2018, [1]),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                provider="cosmo-rea6", range="single-month", year=2018,
                month=1, country="netherlands",
            )
        )
        assert rc == 0
        renamed = tmp_path / "output" / "COSMO_REA6_NL_2018_01_all_attrs.nc"
        original = tmp_path / "output" / "COSMO_REA6_2018_01_all_attrs.nc"
        assert renamed.exists()
        assert not original.exists()

    def test_no_tag_leaves_default_filenames_unchanged(
        self, monkeypatch, tmp_path
    ) -> None:
        """Regression check: a plain (no --country/--bbox) fetch must
        produce byte-identical filenames to before this feature existed
        -- every already-completed archive depends on this."""
        _patch_provider(monkeypatch, "merra-2")
        _install_fake_pipeline(
            monkeypatch,
            "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [3]),
        )
        rc = unified_cli.cmd_fetch(_args(range="single-month", year=2018, month=3))
        assert rc == 0
        assert (tmp_path / "output" / "MERRA2_2018_03_all_attrs.nc").exists()

    def test_concatenate_with_country_tag_uses_tagged_name(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("MERRA2_AREA", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        monthly = _monthly_paths(tmp_path, "merra-2", 2023, list(range(1, 13)))
        _install_fake_pipeline(monkeypatch, "merra-2", lambda **kw: monthly)

        merge_calls = []
        _patch_merge(
            monkeypatch, lambda paths, dest: merge_calls.append(dest.name)
        )
        rc = unified_cli.cmd_fetch(
            _args(
                range="single-year", year=2023, concatenate="all",
                country="netherlands",
            )
        )
        assert rc == 0
        assert merge_calls == ["MERRA2_NL_2023_annual_all_attrs.nc"]

    def test_resume_skips_pipeline_when_all_tagged_files_exist(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("MERRA2_AREA", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        monkeypatch.setattr(
            unified_cli, "_settings_value",
            lambda _c, suffix: (tmp_path / "output") if suffix == "output_dir" else 8,
        )
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        for m in range(1, 13):
            (out_dir / f"MERRA2_NL_2018_{m:02d}_all_attrs.nc").touch()

        calls = _install_fake_pipeline(
            monkeypatch, "merra-2", lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, [1])
        )
        rc = unified_cli.cmd_fetch(
            _args(
                range="single-year", year=2018, country="netherlands", resume=True,
            )
        )
        assert rc == 0
        assert len(calls) == 0  # run_pipeline never called -- all 12 already tagged

    def test_resume_does_not_skip_when_tagged_files_partially_exist(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("MERRA2_AREA", raising=False)
        _patch_provider(monkeypatch, "merra-2")
        monkeypatch.setattr(
            unified_cli, "_settings_value",
            lambda _c, suffix: (tmp_path / "output") if suffix == "output_dir" else 8,
        )
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        # Only 6 of 12 months already tagged-done.
        for m in range(1, 7):
            (out_dir / f"MERRA2_NL_2018_{m:02d}_all_attrs.nc").touch()

        calls = _install_fake_pipeline(
            monkeypatch, "merra-2",
            lambda **kw: _monthly_paths(tmp_path, "merra-2", 2018, list(range(1, 13))),
        )
        rc = unified_cli.cmd_fetch(
            _args(
                range="single-year", year=2018, country="netherlands", resume=True,
            )
        )
        assert rc == 0
        assert len(calls) == 1  # falls through to a real (whole-year) call
