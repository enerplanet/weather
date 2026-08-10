"""Canonical registry of variables ``get_point_weather``/``weather serve``
can return, and the ``use_case`` shorthand that groups them.

Single source of truth for three things that must never drift apart:
point_query.py's extraction logic, the API's ``variables``/``use_case``
query params, and the ``GET /v1/weather/variables`` discovery endpoint a
caller who doesn't already know these names can query first.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VariableSpec:
    name: str
    unit: str
    description: str


# GHI is not independently selectable: DHI/DNI are reconstructed FROM GHI
# (see point_query.py's DISC/DIRINT call), and a caller asking for DHI/DNI
# without GHI would get an irradiance split with nothing to split -- GHI is
# pulled automatically whenever DHI or DNI is requested (see
# point_query.py's need_solar), so it isn't listed as its own use_case
# member, but remains independently queryable via variables=GHI directly.
VARIABLES: dict[str, VariableSpec] = {
    "T": VariableSpec("T", "degC", "Air temperature at 2m"),
    "GHI": VariableSpec("GHI", "W/m2", "Global horizontal irradiance"),
    "DHI": VariableSpec(
        "DHI", "W/m2", "Diffuse horizontal irradiance, reconstructed from GHI"
    ),
    "DNI": VariableSpec(
        "DNI", "W/m2", "Direct normal irradiance, reconstructed from GHI"
    ),
    "WS_10M": VariableSpec(
        "WS_10M", "m/s", "Scalar wind speed at 10m, sqrt(U_10M^2 + V_10M^2)"
    ),
    "U_10M": VariableSpec("U_10M", "m/s", "Eastward wind component at 10m"),
    "V_10M": VariableSpec("V_10M", "m/s", "Northward wind component at 10m"),
}

# Deliberately conservative: only variables a technology's own model
# strictly needs, not everything that might plausibly be useful (e.g. T
# for wind's air-density corrections) -- that's a modeling decision
# belonging to whoever builds that consumer, not something to guess at
# here without one to validate against. A caller wanting T alongside wind
# can already ask for it directly via variables=WS_10M,U_10M,V_10M,T.
USE_CASES: dict[str, tuple[str, ...]] = {
    "solar": ("T", "GHI", "DHI", "DNI"),
    "wind": ("WS_10M", "U_10M", "V_10M"),
}

def resolve_variables(
    variables: str | list[str] | tuple[str, ...] | None = None,
    use_case: str | None = None,
) -> tuple[str, ...]:
    """Resolve a caller's ``variables``/``use_case`` request to a concrete,
    validated tuple of canonical variable names.

    Exactly one of *variables*/*use_case* is required -- no default. A
    caller that forgets to say what it needs should get a clear error,
    not a silently-substituted guess it might not notice is wrong (same
    reasoning as BuEM's own required-weather change).

    Raises
    ------
    ValueError
        If neither or both are given, or either names something not in
        ``VARIABLES``/``USE_CASES`` -- the bad value(s) are named in the
        message, not just "invalid request".
    """
    if variables is not None and use_case is not None:
        raise ValueError(
            "Provide at most one of variables/use_case, not both "
            f"(got variables={variables!r}, use_case={use_case!r})"
        )

    if use_case is not None:
        if use_case not in USE_CASES:
            available = ", ".join(sorted(USE_CASES))
            raise ValueError(f"Unknown use_case: {use_case!r}. Available: {available}")
        return USE_CASES[use_case]

    if variables is not None:
        names = variables.split(",") if isinstance(variables, str) else list(variables)
        names = [n.strip() for n in names if n.strip()]
        unknown = [n for n in names if n not in VARIABLES]
        if unknown:
            available = ", ".join(sorted(VARIABLES))
            raise ValueError(
                f"Unknown variable(s): {unknown!r}. Available: {available}"
            )
        if not names:
            raise ValueError("variables was given but empty")
        return tuple(names)

    available = ", ".join(sorted(USE_CASES))
    raise ValueError(
        "Specify one of variables/use_case -- e.g. use_case=solar or "
        f"variables=T,GHI,DHI,DNI. Available use_case names: {available}"
    )
