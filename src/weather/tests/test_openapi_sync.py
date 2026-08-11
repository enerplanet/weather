"""Guards docs/openapi.yaml's use_case/variables enums against silently
drifting from weather.variables -- the spec has no build step, so nothing
else would catch a param added to one but not the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from weather.variables import USE_CASES, VARIABLES  # noqa: E402

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml"


def _point_query_param(name: str) -> dict:
    spec = yaml.safe_load(OPENAPI_PATH.read_text())
    params = spec["paths"]["/v1/weather/point"]["get"]["parameters"]
    return next(p for p in params if p["name"] == name)


def test_openapi_exists():
    assert OPENAPI_PATH.is_file(), OPENAPI_PATH


def test_use_case_enum_matches_registry():
    enum = _point_query_param("use_case")["schema"]["enum"]
    assert set(enum) == set(USE_CASES)


def test_variables_enum_matches_registry():
    enum = _point_query_param("variables")["schema"]["items"]["enum"]
    assert set(enum) == set(VARIABLES)


def test_openapi_spec_is_valid():
    """The spec itself is well-formed OpenAPI 3.0.3, not just internally
    consistent with weather.variables (the two checks above)."""
    openapi_spec_validator = pytest.importorskip("openapi_spec_validator")

    spec = yaml.safe_load(OPENAPI_PATH.read_text())
    openapi_spec_validator.validate(spec)
