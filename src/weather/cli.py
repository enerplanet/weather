"""Command-line interface for the weather processing pipeline.

Entry points
------------
* ``python -m weather``          → calls :func:`main`
* ``weather`` (installed script) → calls :func:`main`  (see pyproject.toml)

Subcommands
-----------
info       Show resolved provider configuration and attribute definitions.
validate   Check that all required tools and libraries are present.
run        Execute the full download → decompress → transform →
           export pipeline.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_provider_arg(parser: argparse.ArgumentParser) -> None:
    """Attach ``--provider`` to a subcommand parser."""
    from .registry import list_providers  # deferred — avoids circular import

    parser.add_argument(
        "--provider",
        default=None,
        metavar="NAME",
        help=(
            "Weather provider. "
            f"Available: {', '.join(list_providers())}. "
            "Defaults to WEATHER_PROVIDER env var or 'cosmo-rea6'."
        ),
    )


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="weather",
        description="Weather data processing pipeline (provider-based).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m weather info\n"
            "  python -m weather info --provider merra-2\n"
            "  python -m weather validate\n"
            "  python -m weather run --year 2020 --months 1 2 3\n"
            "  python -m weather run --provider cosmo-rea6 --skip-download\n"
            "  python -m weather geo list\n"
            "  python -m weather geo crop --input ERA5_Land_2020_06.nc \\\n"
            "      --output ERA5_Land_2020_06_netherlands.nc "
            "--country netherlands\n"
            "  python -m weather fetch --range single-month --year 2018 "
            "--month 3\n"
            "\n"
            "Installed conda package only:\n"
            "  weather info\n"
            "  weather validate\n"
            "  weather run --provider cosmo-rea6 --months 1\n"
            "  weather fetch --range single-month --year 2018 --month 3\n"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ── info ────────────────────────────────────────────────────────────────
    info_p = sub.add_parser(
        "info",
        help="Show resolved provider configuration.",
    )
    _add_provider_arg(info_p)

    # ── validate ────────────────────────────────────────────────────────────
    validate_p = sub.add_parser(
        "validate",
        help="Check required tools and libraries.",
    )
    _add_provider_arg(validate_p)

    # ── run ─────────────────────────────────────────────────────────────────
    run_p = sub.add_parser(
        "run",
        help="Execute the full pipeline.",
    )
    _add_provider_arg(run_p)
    run_p.add_argument(
        "--year",
        type=int,
        default=None,
        help="Year to process (e.g. 2020).",
    )
    run_p.add_argument(
        "--months",
        type=int,
        nargs="+",
        default=None,
        metavar="M",
        help=(
            "Subset of months to process (1-12). "
            "ERA5-Land only; COSMO-REA6 processes the full year."
        ),
    )
    run_p.add_argument(
        "--ncores",
        type=int,
        default=None,
        metavar="N",
        help="Worker count (ERA5-Land transform / COSMO phases).",
    )
    run_p.add_argument(
        "--work-dir",
        default=None,
        metavar="DIR",
        help="Override provider work directory.",
    )
    run_p.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Output NetCDF file path (COSMO-REA6 only).",
    )
    run_p.add_argument(
        "--no-wind-components",
        action="store_true",
        help="Exclude raw U_10M / V_10M from output (COSMO-REA6 only).",
    )
    run_p.add_argument(
        "--no-night-mask",
        action="store_true",
        help="Disable Spencer SZA night-masking of GHI (ERA5-Land only).",
    )
    run_p.add_argument(
        "--complevel",
        type=int,
        default=1,
        metavar="N",
        help="zlib compression level 1-9 (default: 1).",
    )
    run_p.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip download step (assume files already present).",
    )
    run_p.add_argument(
        "--skip-decompress",
        action="store_true",
        help=(
            "Skip decompression step (COSMO-REA6 only; ERA5-Land has "
            "no decompress phase)."
        ),
    )
    run_p.add_argument(
        "--resume",
        action="store_true",
        help="Skip months/years whose output already exists.",
    )
    run_p.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove downloaded / decompressed files after export.",
    )

    # ── geo ─────────────────────────────────────────────────────────────────
    geo_p = sub.add_parser(
        "geo",
        help="Country bounding-box lookup and NetCDF cropping.",
    )
    geo_sub = geo_p.add_subparsers(dest="geo_command", metavar="GEO_COMMAND")

    geo_crop_p = geo_sub.add_parser(
        "crop",
        help="Crop a NetCDF file to a country's bounding box via cdo.",
    )
    geo_crop_p.add_argument(
        "--input",
        required=True,
        metavar="PATH",
        help="Input NetCDF file (any provider's output).",
    )
    geo_crop_p.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Output path for the cropped NetCDF.",
    )
    geo_crop_p.add_argument(
        "--country",
        required=True,
        metavar="NAME",
        help="Country name, e.g. 'netherlands' (see 'weather geo list').",
    )

    geo_sub.add_parser(
        "list",
        help="List all recognised country names.",
    )

    # ── serve ───────────────────────────────────────────────────────────────
    serve_p = sub.add_parser(
        "serve",
        help="Run the point-query HTTP API (see src/weather/api/README.md).",
    )
    serve_p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    serve_p.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080).")

    # ── fetch ───────────────────────────────────────────────────────────────
    from .unified_cli import add_fetch_parser  # deferred — avoids circular import

    add_fetch_parser(sub)

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_info(args: argparse.Namespace) -> None:
    from .registry import get_default_provider, get_provider

    provider = (
        get_provider(args.provider)
        if args.provider
        else get_default_provider()
    )
    cfg = provider.get_config_summary()

    print(f"Weather Pipeline — Provider: {provider.name}")
    print("=" * 56)
    for key in sorted(cfg.keys()):
        print(f"  {key:<22s}  {cfg[key]}")

    if provider.name == "cosmo-rea6":
        from .providers.cosmo_rea6.downloaded_attributes import ATTRIBUTES

        print()
        print("Attribute definitions:")
        for attr_name, meta in ATTRIBUTES.items():
            print(
                f"  {attr_name:<16s}  "
                f"{meta['unit_raw']:>6s} -> {meta['unit_target']:<6s}  "
                f"{meta['description']}"
            )


def _cmd_validate(args: argparse.Namespace) -> int:
    from .providers.base import ADVISORY_PREFIX
    from .registry import get_default_provider, get_provider

    provider = (
        get_provider(args.provider)
        if args.provider
        else get_default_provider()
    )
    issues = provider.validate_environment()
    advisories = [
        i[len(ADVISORY_PREFIX):] for i in issues
        if i.startswith(ADVISORY_PREFIX)
    ]
    critical = [i for i in issues if not i.startswith(ADVISORY_PREFIX)]

    if advisories:
        print(
            "Weather pipeline environment advisories "
            "(degraded, not broken):"
        )
        for issue in advisories:
            print(f"  [i] {issue}")
    if critical:
        print("Weather pipeline environment issues found:")
        for issue in critical:
            print(f"  [!] {issue}")
        return 1
    suffix = " (see advisories above)." if advisories else "."
    print("Weather pipeline environment OK — all checks passed" + suffix)
    return 0


def _cmd_run(args: argparse.Namespace) -> None:
    from .registry import get_default_provider, get_provider

    provider = (
        get_provider(args.provider)
        if args.provider
        else get_default_provider()
    )

    work_dir = Path(args.work_dir) if args.work_dir else None

    # Build provider-specific kwargs.  ERA5-Land has no decompress step
    # and a per-month/single-folder output, so its run_pipeline takes a
    # different signature than COSMO-REA6.
    if provider.name == "era5-land":
        kwargs = {
            "year": args.year,
            "months": args.months,
            "work_dir": work_dir,
            "ncores": args.ncores,
            "night_mask": not args.no_night_mask,
            "complevel": args.complevel,
            "skip_download": args.skip_download,
            "resume": args.resume,
            "cleanup": args.cleanup,
        }
    else:
        # COSMO-REA6 (and providers sharing its signature).
        kwargs = {
            "year": args.year,
            "months": args.months,
            "ncores": args.ncores,
            "work_dir": work_dir,
            "output_path": Path(args.output) if args.output else None,
            "include_wind_components": not args.no_wind_components,
            "complevel": args.complevel,
            "skip_download": args.skip_download,
            "skip_decompress": args.skip_decompress,
            "cleanup": args.cleanup,
        }

    nc_path = provider.run_pipeline(**kwargs)
    print(f"\nOutput: {nc_path}")


def _cmd_geo(args: argparse.Namespace) -> int:
    from .geo import crop_to_country, list_countries

    if args.geo_command == "list":
        for name in list_countries():
            print(name)
        return 0

    if args.geo_command == "crop":
        try:
            crop_to_country(Path(args.input), Path(args.output), args.country)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}")
            return 1
        print(f"Cropped: {args.output}")
        return 0

    print("Usage: weather geo {crop,list}")
    return 1


def _cmd_serve(args: argparse.Namespace) -> None:
    try:
        from .api import create_app
    except ImportError as exc:
        raise SystemExit(
            "The `api` extra is required: pip install weather[api]"
        ) from exc

    app = create_app()
    print(
        f"Weather point-query API on http://{args.host}:{args.port} "
        "-- dev server only, use a real WSGI server (gunicorn) in production."
    )
    app.run(host=args.host, port=args.port)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "info":
        _cmd_info(args)
    elif args.command == "validate":
        sys.exit(_cmd_validate(args))
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "geo":
        sys.exit(_cmd_geo(args))
    elif args.command == "serve":
        _cmd_serve(args)
    elif args.command == "fetch":
        from .unified_cli import cmd_fetch

        sys.exit(cmd_fetch(args))
    else:
        parser.print_help()
        sys.exit(1)
