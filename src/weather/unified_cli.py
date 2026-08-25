"""``weather fetch`` -- one command across all three providers.

Wires together machinery that already exists but was never connected:
``run_pipeline()`` (per provider), ``weather.common.merge.NetCDFMerger``
(concatenation), each provider's ``*PercentileIndexer`` (percentile), and
``weather.geo`` (country/bbox-scoped downloads). No new business logic
lives here -- only flag parsing, validation, and dispatch to those
existing functions/classes.

Calls the raw ``weather.providers.<x>.pipeline.run_pipeline()`` functions
directly rather than the ``registry``-returned provider facades
(``CosmoREA6Provider``/``ERA5LandProvider``/``MERRA2Provider``): every
facade's own ``run_pipeline()`` ends with
``return outputs[0] if outputs else Path()``, discarding every month but
the first, which makes them unusable here -- ``--concatenate`` needs the
full ``list[Path]`` of monthly outputs. ``registry.get_provider()`` is
still used for provider-name/alias resolution and its existing
``ValueError`` message, just not for the pipeline call itself.

Deliberately does NOT touch any of the per-provider
``test_<provider>_{one_month,one_year,multi_year}.py`` scripts or the
production bulk-run shell scripts that invoke them -- this module is
purely additive.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .common.cli_flags import (
    add_resume_flag,
    add_skip_decompress_flag,
    add_skip_download_flag,
)

if TYPE_CHECKING:
    from .geo.bbox import BBox

# ---------------------------------------------------------------------------
# Per-provider metadata needed for dispatch
# ---------------------------------------------------------------------------

#: canonical provider name -> EnvSettings method prefix (e.g. "era5_from_year")
_SETTINGS_PREFIX = {
    "cosmo-rea6": "cosmo",
    "era5-land": "era5",
    "merra-2": "merra2",
}

#: canonical provider name -> output filename prefix
_FILE_PREFIX = {
    "cosmo-rea6": "COSMO_REA6",
    "era5-land": "ERA5_LAND",
    "merra-2": "MERRA2",
}

#: canonical provider name -> area env var, for the two providers with a
#: real server-side area-subsetting endpoint. cosmo-rea6 has none -- DWD
#: always serves the whole fixed domain -- so it uses a local crop_bbox
#: kwarg instead (see the "area scoping" block in cmd_fetch()).
_AREA_ENV_VAR = {
    "era5-land": "ERA5_AREA",
    "merra-2": "MERRA2_AREA",
}

#: canonical provider name -> region-tag env var, set alongside
#: _AREA_ENV_VAR so the downloader's local_path() can disambiguate a
#: country-scoped run's raw download filename from a different
#: country's (see downloader.py's local_path/content_key in both
#: providers) -- internal plumbing, not documented in .env.example.
_REGION_TAG_ENV_VAR = {
    "era5-land": "ERA5_REGION_TAG",
    "merra-2": "MERRA2_REGION_TAG",
}

#: canonical provider name -> pipeline module (raw run_pipeline(), not facade)
_PIPELINE_MODULE = {
    "cosmo-rea6": "weather.providers.cosmo_rea6.pipeline",
    "era5-land": "weather.providers.era5_land.pipeline",
    "merra-2": "weather.providers.merra2.pipeline",
}

#: canonical provider name -> (percentile module, indexer class name)
_PERCENTILE_CLASS = {
    "cosmo-rea6": (
        "weather.providers.cosmo_rea6.percentile_index",
        "CosmoRea6PercentileIndexer",
    ),
    "era5-land": (
        "weather.providers.era5_land.percentile_index",
        "Era5LandPercentileIndexer",
    ),
    "merra-2": (
        "weather.providers.merra2.percentile_index",
        "Merra2PercentileIndexer",
    ),
}


def _settings_value(canonical: str, suffix: str) -> Any:
    """Return ``EnvSettings.<prefix>_<suffix>()`` for *canonical*'s
    provider."""
    from .settings import EnvSettings

    prefix = _SETTINGS_PREFIX[canonical]
    method = getattr(EnvSettings, f"{prefix}_{suffix}")
    return method()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _FetchHelpFormatter(
    argparse.RawDescriptionHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter,
):
    """Real examples in the epilog (Raw) plus auto ``(default: ...)``
    per flag."""


def add_fetch_parser(sub: Any) -> argparse.ArgumentParser:
    """Attach the ``fetch`` subparser to *sub* (``cli.py``'s subparsers
    action)."""
    fetch_p = sub.add_parser(
        "fetch",
        formatter_class=_FetchHelpFormatter,
        help=(
            "Download a date range for one provider -- optionally "
            "concatenated and/or percentile-indexed, optionally "
            "scoped to a country/bbox."
        ),
        epilog=(
            "Examples:\n"
            "  weather fetch --range single-month --year 2018 --month 3\n"
            "  weather fetch --provider merra-2 --range single-year "
            "--year 2023 \\\n"
            "      --country netherlands --concatenate all\n"
            "  weather fetch --provider era5-land --range multi-year \\\n"
            "      --from-year 2015 --to-year 2020 --concatenate per-year\n"
            "  weather fetch --provider cosmo-rea6 --range single-year "
            "--year 2018 \\\n"
            "      --percentile --which-percentile p50\n"
        ),
    )

    fetch_p.add_argument(
        "--provider",
        default=None,
        metavar="NAME",
        help=(
            "Weather provider. Available: cosmo-rea6, era5-land, merra-2 "
            "(aliases: cosmo, era5, merra2). Defaults to WEATHER_PROVIDER "
            "env var or cosmo-rea6."
        ),
    )
    fetch_p.add_argument(
        "--range",
        required=True,
        choices=["single-month", "single-year", "multi-year"],
        help="Time span to fetch.",
    )

    fetch_p.add_argument(
        "--year",
        type=int,
        default=None,
        metavar="YEAR",
        help=(
            "Year. Required for --range single-month; optional for "
            "single-year (defaults to the provider's configured year)."
        ),
    )
    fetch_p.add_argument(
        "--month",
        type=int,
        default=None,
        choices=range(1, 13),
        metavar="M",
        help="Month 1-12. Required iff --range single-month.",
    )
    fetch_p.add_argument(
        "--from-year",
        type=int,
        default=None,
        metavar="YEAR",
        help=(
            "First year. Required iff --range multi-year (defaults to "
            "the provider's configured from-year)."
        ),
    )
    fetch_p.add_argument(
        "--to-year",
        type=int,
        default=None,
        metavar="YEAR",
        help=(
            "Last year, inclusive. Required iff --range multi-year "
            "(defaults to the provider's configured to-year)."
        ),
    )

    fetch_p.add_argument(
        "--ncores", type=int, default=None, metavar="N", help="Worker count."
    )
    fetch_p.add_argument(
        "--work-dir",
        default=None,
        metavar="DIR",
        help="Override the provider's work directory (where output/ lives).",
    )
    fetch_p.add_argument(
        "--complevel",
        type=int,
        default=1,
        metavar="N",
        help="zlib compression level 1-9.",
    )

    add_skip_download_flag(fetch_p)
    add_skip_decompress_flag(fetch_p)  # validated: cosmo-rea6 only
    fetch_p.add_argument(
        "--skip-dni",
        action="store_true",
        help="Skip experimental per-cell DNI computation (cosmo-rea6 only).",
    )
    fetch_p.add_argument(
        "--night-mask",
        action="store_true",
        help="Enable Spencer SZA night-masking of GHI (era5-land only).",
    )
    add_resume_flag(fetch_p)
    fetch_p.add_argument(
        "--cleanup",
        action="store_true",
        default=None,
        help=(
            "Remove downloaded/decompressed intermediates after export "
            "(default: the provider's own *_CLEANUP env setting)."
        ),
    )

    fetch_p.add_argument(
        "--concatenate",
        choices=["none", "per-year", "all"],
        default="none",
        help=(
            "Merge monthly output into one file per year (per-year) or "
            "one file across the whole range (all). For --range "
            "single-year, per-year and all are equivalent (only one year "
            "exists). Not valid for --range single-month."
        ),
    )
    fetch_p.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help=(
            "Destination path for the concatenated file (only valid "
            "when --concatenate produces exactly one file)."
        ),
    )

    fetch_p.add_argument(
        "--percentile",
        action="store_true",
        help=(
            "Compute P10/P50/P90 representative-year mosaics from the "
            "provider's full historical output/ folder after fetching."
        ),
    )
    fetch_p.add_argument(
        "--which-percentile",
        nargs="+",
        choices=["p10", "p50", "p90"],
        default=None,
        metavar="PCT",
        help=(
            "Which percentile(s) to highlight in the summary. All three "
            "are always computed and written (the shared indexer computes "
            "them in one fused pass) -- this only filters what's "
            "reported, it does not skip or delete anything. "
            "Default: report all three."
        ),
    )

    from .geo.countries import list_countries

    area_group = fetch_p.add_mutually_exclusive_group()
    area_group.add_argument(
        "--country",
        default=None,
        metavar="NAME",
        choices=sorted(list_countries()),
        help=(
            "Crop the download to a country's bounding box. Real "
            "pre-download server-side subsetting for era5-land/merra-2; "
            "a local post-decompress crop for cosmo-rea6 (DWD has no "
            "server-side area subsetting -- download/decompress still "
            "fetch the whole domain, only transform onward is cropped). "
            "Output filenames get an ISO 3166-1 country-code tag (e.g. "
            "NL) inserted after the provider prefix. Available: "
            + ", ".join(sorted(list_countries()))
        ),
    )
    area_group.add_argument(
        "--bbox",
        default=None,
        metavar="N,W,S,E",
        help=(
            "Crop the download to a custom bounding box, degrees "
            "North,West,South,East. Same scoping mechanism as "
            "--country (see its help) for whichever provider is "
            "selected. Output filenames get a CUSTOM tag inserted "
            "after the provider prefix (no short code exists for an "
            "arbitrary box)."
        ),
    )

    return fetch_p


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> int:  # noqa: PLR0911, PLR0912
    """Handle the ``weather fetch`` subcommand. Returns a process exit code."""
    from .registry import get_provider

    try:
        provider = get_provider(args.provider)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    canonical = provider.name

    # ── range-specific validation / defaulting ──────────────────────────
    if args.range == "single-month":
        if args.year is None or args.month is None:
            print("Error: --range single-month requires --year and --month.")
            return 1
        if args.concatenate != "none":
            print(
                "Error: --concatenate is not valid for "
                "--range single-month."
            )
            return 1
    elif args.range == "single-year":
        if args.year is None:
            args.year = _settings_value(canonical, "year")
    else:  # multi-year
        if args.from_year is None:
            args.from_year = _settings_value(canonical, "from_year")
        if args.to_year is None:
            args.to_year = _settings_value(canonical, "to_year")
        if args.concatenate == "per-year" and args.output:
            print(
                "Error: --output is not valid with --concatenate per-year "
                "(produces multiple files); omit --output or use "
                "--concatenate all."
            )
            return 1

    # ── provider-specific flag validation ───────────────────────────────
    if args.skip_decompress and canonical != "cosmo-rea6":
        print(
            f"Error: --skip-decompress is cosmo-rea6 only (got {canonical})."
        )
        return 1
    if args.skip_dni and canonical != "cosmo-rea6":
        print(f"Error: --skip-dni is cosmo-rea6 only (got {canonical}).")
        return 1
    if args.night_mask and canonical != "era5-land":
        print(f"Error: --night-mask is era5-land only (got {canonical}).")
        return 1

    # ── area scoping ─────────────────────────────────────────────────────
    # era5-land/merra-2: real pre-download SERVER-SIDE subsetting, via the
    # same ERA5_AREA/MERRA2_AREA env var each provider's get_config()
    # already reads fresh on every call (never cached at import).
    # cosmo-rea6: DWD has no server-side area-subsetting endpoint at all
    # -- every download is always whole-domain -- so this instead becomes
    # a LOCAL crop_bbox kwarg applied after decompress, before transform
    # (see providers/cosmo_rea6/crop.py). Both paths still get the same
    # region_tag treatment for output filenames below.
    region_tag: str | None = None
    crop_bbox: BBox | None = None
    if args.country or args.bbox:
        from .geo.bbox import BBox as _BBox
        from .geo.countries import get_bbox, get_country_code

        try:
            if args.country:
                bbox = get_bbox(args.country)
                region_tag = get_country_code(args.country)
            else:
                bbox = _BBox.parse(args.bbox)
                # "CUSTOM" alone collides across different bboxes for
                # the same year/month (they'd share both the download
                # AND output filename tag) -- a short deterministic
                # hash of the actual bbox values keeps the readable
                # prefix while making two different --bbox values
                # produce disjoint tags.
                bbox_hash = hashlib.sha256(
                    str(tuple(bbox)).encode()
                ).hexdigest()[:6]
                region_tag = f"CUSTOM-{bbox_hash}"
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1
        if canonical == "cosmo-rea6":
            crop_bbox = bbox
            print(
                f"Crop bbox: {bbox} (tag: {region_tag}) -- cosmo-rea6 "
                "has no server-side area subsetting; applied locally "
                "after decompress, before transform"
            )
        else:
            env_var = _AREA_ENV_VAR[canonical]
            os.environ[env_var] = ",".join(
                str(v) for v in bbox.to_area_list()
            )
            # Also tags the raw download-phase filename (see
            # downloader.py's local_path()), not just the output file
            # -- without this, a second country/bbox run against the
            # same work_dir can silently reuse the FIRST region's
            # cached raw download for the same year/month (fixed
            # 2026-08-15; see docs/WEATHER_FETCH_GUIDE.md's "Region-
            # scoped download caching" section).
            os.environ[_REGION_TAG_ENV_VAR[canonical]] = region_tag
            print(
                f"Area override: {env_var}={os.environ[env_var]} "
                f"(tag: {region_tag})"
            )

    work_dir = Path(args.work_dir) if args.work_dir else None
    pipeline_mod = importlib.import_module(_PIPELINE_MODULE[canonical])

    def _kwargs(year: int, months: list[int] | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "year": year,
            "months": months,
            "work_dir": work_dir,
            "ncores": args.ncores,
            "complevel": args.complevel,
            "skip_download": args.skip_download,
            "resume": args.resume,
            "cleanup": args.cleanup,
        }
        if canonical == "cosmo-rea6":
            kwargs["skip_decompress"] = args.skip_decompress
            kwargs["skip_dni"] = args.skip_dni
            kwargs["crop_bbox"] = crop_bbox
        elif canonical == "era5-land":
            kwargs["night_mask"] = args.night_mask
        return kwargs

    def _fetch(year: int, months: list[int] | None) -> list[Path]:
        """Run one fetch, applying *region_tag* to the result.

        If --resume + a region tag are both active, skips the real
        run_pipeline() call when every expected TAGGED file already
        exists. run_pipeline()'s own --resume only checks the untagged
        path, which no longer exists once tagged (see _tag_path()) --
        without this, a re-run would silently redo already-finished
        country-scoped work every time.

        This check is all-or-nothing per year (all requested months'
        tagged files must exist, or none of them count) -- if only
        SOME months are already tagged-done, this falls through to a
        fresh run_pipeline() call for the whole year, which will
        needlessly redo those already-finished months too (their
        untagged path was renamed away, so run_pipeline()'s own
        per-month resume check can't see them either). Full per-month
        resume awareness for tagged runs would need run_pipeline() to
        accept an explicit subset of remaining months; out of scope
        here -- this covers the common case (a whole tagged year is
        either fully done or not started) correctly.
        """
        if region_tag and args.resume:
            check_months = months or list(range(1, 13))
            expected = _expected_tagged_paths(
                canonical, year, check_months, region_tag, work_dir
            )
            if all(p.exists() for p in expected):
                print(
                    f"  [resume] {canonical} {year}: all tagged file(s) "
                    "already exist, skipping"
                )
                return expected
        outputs = pipeline_mod.run_pipeline(**_kwargs(year, months))
        return _apply_region_tag(outputs, region_tag, canonical)

    # ── fetch ────────────────────────────────────────────────────────────
    if args.range == "single-month":
        outputs = _fetch(args.year, [args.month])
        _report(outputs, f"{canonical} {args.year}-{args.month:02d}")

    elif args.range == "single-year":
        outputs = _fetch(args.year, None)
        _report(outputs, f"{canonical} {args.year}")
        if args.concatenate != "none":
            _concatenate(
                canonical, outputs, args.output, year=args.year,
                region_tag=region_tag,
            )

    else:  # multi-year
        by_year: dict[int, list[Path]] = {}
        for yr in range(args.from_year, args.to_year + 1):
            print(f"\n=== {canonical} {yr} ===")
            by_year[yr] = _fetch(yr, None)
            print(f"  {len(by_year[yr])} file(s)")
        if args.concatenate == "per-year":
            for yr, outs in by_year.items():
                _concatenate(
                    canonical, outs, output_override=None, year=yr,
                    region_tag=region_tag,
                )
        elif args.concatenate == "all":
            flat = [p for yr in sorted(by_year) for p in by_year[yr]]
            _concatenate(
                canonical,
                flat,
                args.output,
                from_year=args.from_year,
                to_year=args.to_year,
                region_tag=region_tag,
            )

    if args.percentile:
        _run_percentile(canonical, args.ncores, args.which_percentile)

    return 0


def _report(outputs: list[Path], label: str) -> None:
    print(f"\nFetched {len(outputs)} file(s) for {label}:")
    for p in outputs:
        print(f"  {p}")


def _tag_path(path: Path, tag: str, prefix: str) -> Path:
    """Rename *path* to insert ``_{tag}`` right after the provider's
    filename prefix (e.g. ``MERRA2_2018_01_all_attrs.nc`` ->
    ``MERRA2_NL_2018_01_all_attrs.nc``), returning the new path.

    Only the FIRST occurrence of ``{prefix}_`` is replaced, so this
    stays correct regardless of the exact suffix pattern (monthly vs
    annual vs range) rather than needing to reconstruct filenames from
    scratch. A no-op (no rename) if the name doesn't start with
    ``prefix`` -- defensive, should not happen with any real provider
    output.
    """
    new_name = path.name.replace(f"{prefix}_", f"{prefix}_{tag}_", 1)
    if new_name == path.name:
        return path
    new_path = path.with_name(new_name)
    path.replace(new_path)
    return new_path


def _apply_region_tag(
    outputs: list[Path], region_tag: str | None, canonical: str
) -> list[Path]:
    """Rename every path in *outputs* to include *region_tag*, if set."""
    if not region_tag:
        return outputs
    prefix = _FILE_PREFIX[canonical]
    return [_tag_path(p, region_tag, prefix) for p in outputs]


def _expected_tagged_paths(
    canonical: str,
    year: int,
    months: list[int],
    region_tag: str,
    work_dir: Path | None,
) -> list[Path]:
    """The tagged monthly filenames a fetch for *year*/*months* would
    produce, without actually running the pipeline -- used to check
    whether --resume can skip the real call entirely (see ``_fetch()``
    in ``cmd_fetch()``)."""
    out_dir = (
        (work_dir / "output") if work_dir
        else _settings_value(canonical, "output_dir")
    )
    prefix = _FILE_PREFIX[canonical]
    return [
        out_dir / f"{prefix}_{region_tag}_{year}_{m:02d}_all_attrs.nc"
        for m in months
    ]


def _concatenate(
    canonical: str,
    monthly_paths: list[Path],
    output_override: str | None,
    *,
    year: int | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    region_tag: str | None = None,
) -> None:
    """Merge *monthly_paths* via ``NetCDFMerger`` into one annual/range
    file."""
    from .common.merge import NetCDFMerger

    label = f"{year}" if year is not None else f"{from_year}-{to_year}"
    if not monthly_paths:
        print(
            f"  (nothing to concatenate for {label} -- "
            "no monthly files produced)"
        )
        return

    out_dir = monthly_paths[0].parent
    prefix = _FILE_PREFIX[canonical]
    tag_part = f"{region_tag}_" if region_tag else ""
    if output_override:
        dest = Path(output_override)
    elif year is not None:
        dest = out_dir / f"{prefix}_{tag_part}{year}_annual_all_attrs.nc"
    else:
        dest = out_dir / (
            f"{prefix}_{tag_part}{from_year}_{to_year}_all_attrs.nc"
        )

    print(f"  Concatenating {len(monthly_paths)} file(s) ({label}) -> {dest}")
    NetCDFMerger.merge(
        monthly_paths=[str(p) for p in sorted(monthly_paths)],
        output_path=dest,
    )


def _run_percentile(
    canonical: str, ncores: int | None, which: list[str] | None
) -> None:
    module_path, cls_name = _PERCENTILE_CLASS[canonical]
    cls = getattr(importlib.import_module(module_path), cls_name)

    source_dir = str(_settings_value(canonical, "output_dir"))
    target_dir = str(Path(source_dir) / "percentile")
    n_cores = ncores or _settings_value(canonical, "ncores")

    print(f"\nRunning percentile index for {canonical} -> {target_dir}")
    indexer = cls(
        source_dir=source_dir, target_dir=target_dir, n_cpu_cores=n_cores
    )
    indexer.execute_indexing_pipeline()

    reported = which or ["p10", "p50", "p90"]
    reported_str = ", ".join(p.upper() for p in reported)
    print(
        f"Percentile complete. Requested: {reported_str} "
        f"(all three are always computed -- see {target_dir})"
    )
