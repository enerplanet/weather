"""GET /v1/weather/variables -- variable + use_case discovery."""

from __future__ import annotations

from typing import Any

from flask import jsonify
from flask.views import MethodView


class VariablesView(MethodView):
    def get(self) -> Any:
        from weather.variables import USE_CASES, VARIABLES

        return jsonify(
            variables={
                name: {"unit": spec.unit, "description": spec.description}
                for name, spec in VARIABLES.items()
            },
            use_cases={name: list(members) for name, members in USE_CASES.items()},
        )
