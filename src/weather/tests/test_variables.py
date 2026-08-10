"""Unit tests for weather.variables' resolve_variables (pure logic, no
archive/network dependency -- see test_point_query.py for the extraction
side of variable selection).
"""

from __future__ import annotations

import pytest

from weather.variables import USE_CASES, VARIABLES, resolve_variables


def test_neither_given_raises():
    with pytest.raises(ValueError, match="Specify one of"):
        resolve_variables()


def test_use_case_solar():
    assert resolve_variables(use_case="solar") == USE_CASES["solar"]


def test_use_case_wind():
    assert resolve_variables(use_case="wind") == ("WS_10M", "U_10M", "V_10M")


def test_unknown_use_case_names_the_bad_value():
    with pytest.raises(ValueError, match="hydro"):
        resolve_variables(use_case="hydro")


def test_variables_string_is_comma_split():
    assert resolve_variables(variables="T,WS_10M") == ("T", "WS_10M")


def test_variables_list_passthrough():
    assert resolve_variables(variables=["GHI", "DHI"]) == ("GHI", "DHI")


def test_unknown_variable_names_the_bad_value():
    with pytest.raises(ValueError, match="RAINFALL"):
        resolve_variables(variables="T,RAINFALL")


def test_empty_variables_string_rejected():
    with pytest.raises(ValueError, match="empty"):
        resolve_variables(variables="")


def test_both_variables_and_use_case_rejected():
    with pytest.raises(ValueError, match="at most one"):
        resolve_variables(variables="T", use_case="solar")


def test_every_use_case_member_is_a_known_variable():
    for use_case, members in USE_CASES.items():
        for name in members:
            assert name in VARIABLES, f"{use_case!r} references unknown variable {name!r}"
