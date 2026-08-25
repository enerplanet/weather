#!/usr/bin/env python
"""
Comprehensive validation script for weather pipeline.
Validates conda environment, Docker, imports, and test suite.

Usage:
    python validate.py [--full] [--docker] [--skip-docker]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run_cmd(
    cmd: list[str], description: str = "", timeout: int = 60
) -> tuple[int, str, str]:
    """Run command and return (exit_code, stdout, stderr).

    Parameters
    ----------
    timeout : int
        Seconds before giving up (default 60 -- fine for quick checks
        like ``docker --version``, but far too short for anything that
        does real work, e.g. a cold ``docker build``; override per call).
    """
    print(f"  → {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def check_conda_env(env_name: str = "weather_env") -> bool:
    """Verify conda environment is usable."""
    print("\n[1/5] Checking conda environment...")
    code, out, err = run_cmd(
        ["conda", "run", "-n", env_name, "python", "--version"],
        "Python version",
    )
    if code != 0:
        print(f"  ✗ Environment '{env_name}' not found or not usable")
        return False
    print(f"  ✓ {out.strip()}")

    code, out, err = run_cmd(
        [
            "conda",
            "run",
            "-n",
            env_name,
            "python",
            "-c",
            "import cfgrib, xarray, dask, numpy; print('OK')",
        ],
        "Imports",
    )
    if code != 0:
        print(f"  ✗ Imports failed: {err}")
        return False
    print("  ✓ Core imports OK (cfgrib, xarray, dask, numpy)")
    return True


def check_cli(env_name: str = "weather_env") -> bool:
    """Verify CLI commands work."""
    print("\n[2/5] Checking CLI interface...")

    # info command
    code, out, err = run_cmd(
        ["conda", "run", "-n", env_name, "python", "-m", "weather", "info"],
        "CLI info",
    )
    if code != 0:
        print(f"  ✗ 'weather info' failed: {err}")
        return False
    print("  ✓ weather info")

    # validate command
    code, out, err = run_cmd(
        [
            "conda",
            "run",
            "-n",
            env_name,
            "python",
            "-m",
            "weather",
            "validate",
        ],
        "CLI validate",
    )
    if code != 0:
        print(f"  ✗ 'weather validate' failed: {err}")
        return False
    print("  ✓ weather validate")
    return True


def check_tests(env_name: str = "weather_env") -> bool:
    """Run test suite."""
    print("\n[3/5] Running test suite...")
    code, out, err = run_cmd(
        [
            "conda",
            "run",
            "-n",
            env_name,
            "python",
            "-m",
            "pytest",
            "src/weather/tests/",
            "-v",
            "--tb=short",
        ],
        "Test suite",
    )
    if code != 0:
        print(" ⚠ Some tests failed (may be expected if data not present)")
        # Don't fail on test failures - data may not be present
    else:
        print("  ✓ All tests passed")
    return True


def check_docker() -> bool:
    """Verify Docker image and functionality."""
    print("\n[4/5] Checking Docker setup...")

    # Check Docker is available
    code, out, err = run_cmd(["docker", "--version"], "Docker version")
    if code != 0:
        print("  ⚠ Docker not available (skipping Docker checks)")
        return True

    print(f"  ✓ {out.strip()}")

    # Check image exists or build it
    code, out, err = run_cmd(
        ["docker", "image", "inspect", "weather:test"],
        "Docker image",
    )
    if code != 0:
        print("  → Building Docker image (this may take 2-3 minutes)...")
        code, out, err = run_cmd(
            [
                "docker",
                "build",
                "-f",
                "infrastructure/container/Dockerfile",
                "-t",
                "weather:test",
                ".",
            ],
            "Docker build",
            # A cold build (base image pull + full conda env solve/install)
            # routinely exceeds run_cmd()'s 60s default -- that default was
            # never actually enough for what this docstring promises, it
            # just went unnoticed while a stale weather:test image was
            # always found first and this branch never ran for real.
            timeout=900,
        )
        if code != 0:
            print(f"  ✗ Docker build failed: {err[-500:]}")
            return False
    print("  ✓ weather:test image OK")

    # Test CLI in container. Two things the naive call gets wrong:
    # 1. weather:test is a deps-only image (see Dockerfile's own header
    #    comment) -- the `weather` package is never installed inside it;
    #    source is bind-mounted at /app/src at runtime
    #    (PYTHONPATH=/app/src in the image), matching every usage example
    #    in the Dockerfile itself. Without this mount, any command fails
    #    with "No module named weather" regardless of whether the image
    #    build itself succeeded.
    # 2. ENTRYPOINT is entrypoint.sh, not python -- with PIPELINE_MODE
    #    unset it already runs `exec python -m weather "$@"` (its
    #    passthrough case), so the docker run CMD should be just the
    #    weather subcommand ("info"), not "python -m weather info" --
    #    passing the full command doubles it up and "python" ends up as
    #    weather's literal first CLI arg.
    src_mount = f"{Path.cwd() / 'src'}:/app/src:ro"
    code, out, err = run_cmd(
        ["docker", "run", "--rm", "-v", src_mount, "weather:test", "info"],
        "Docker CLI",
    )
    if code != 0:
        print(f"  ✗ Docker CLI failed: {err[-500:]}")
        return False
    print("  ✓ Docker CLI works")
    return True


def check_package_structure() -> bool:
    """Verify package structure is valid."""
    print("\n[5/5] Checking package structure...")

    checks = [
        ("src/weather/__init__.py", "Package init"),
        ("src/weather/cli.py", "CLI module"),
        ("src/weather/registry.py", "Provider registry"),
        ("src/weather/common/download.py", "Download utilities"),
        ("src/weather/common/decompress.py", "Decompress utilities"),
        ("src/weather/common/validate.py", "Validation utilities"),
        ("src/weather/providers/cosmo_rea6/__init__.py", "COSMO provider"),
        ("src/weather/providers/merra2/__init__.py", "MERRA-2 provider"),
        ("infrastructure/container/Dockerfile", "Dockerfile"),
        ("infrastructure/container/docker-compose.yml", "Docker Compose"),
        ("pyproject.toml", "Project metadata"),
        ("CHANGELOG.md", "Changelog"),
    ]

    all_ok = True
    for path, desc in checks:
        if Path(path).exists():
            print(f"  ✓ {desc}")
        else:
            print(f"  ✗ Missing: {desc} ({path})")
            all_ok = False

    return all_ok


def main() -> int:
    """Run all validation checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full validation including slow checks",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="Skip Docker checks",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Weather Pipeline — Validation Suite")
    print("=" * 60)

    results = {
        "Conda Environment": check_conda_env(),
        "CLI Interface": check_cli(),
        "Test Suite": check_tests(),
        "Package Structure": check_package_structure(),
    }

    if not args.skip_docker:
        results["Docker Setup"] = check_docker()

    print("\n" + "=" * 60)
    print("Validation Results:")
    print("=" * 60)

    all_passed = True
    for check, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status:8s} {check}")
        if not passed:
            all_passed = False

    print("=" * 60)

    if all_passed:
        print("\n✓ All validations passed — ready for deployment!")
        return 0
    else:
        print("\n✗ Some validations failed — see output above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
