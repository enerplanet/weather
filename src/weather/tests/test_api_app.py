"""Unit tests for the ``weather serve`` Flask app (src/weather/api/app.py).

``weather.get_point_weather`` is monkeypatched -- these exercise the HTTP
layer (auth, format switch, NaN handling), not the point-query pipeline
itself (see test_point_query.py for that).

Run with::

    conda run -n weather_env pytest src/weather/tests/test_api_app.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

flask = pytest.importorskip("flask")

from weather.api.app import create_app  # noqa: E402

API_KEY = "test-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEYS", API_KEY)
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _fake_weather_df() -> pd.DataFrame:
    """All queryable columns -- the route's own `variables` resolution
    selects the requested subset, matching real get_point_weather
    behavior closely enough to test the HTTP layer without needing real
    archive data (see test_point_query.py for the real extraction path).
    """
    index = pd.date_range("2018-01-01", periods=3, freq="h", tz=None, name="time")
    return pd.DataFrame(
        {
            "T": [1.0, 2.0, np.nan],
            "GHI": [0.0, 100.0, 200.0],
            "DHI": [0.0, 50.0, 60.0],
            "DNI": [0.0, 300.0, 400.0],
            "WS_10M": [3.0, 4.0, 5.0],
            "U_10M": [1.0, 1.5, 2.0],
            "V_10M": [2.0, 2.5, 3.0],
        },
        index=index,
    )


def _patch_get_point_weather(monkeypatch):
    import weather

    def _fake(*args, **kwargs):
        variables = kwargs.get("variables") or ("T", "GHI", "DHI", "DNI")
        return _fake_weather_df()[list(variables)]

    monkeypatch.setattr(weather, "get_point_weather", _fake)


def test_weather_point_json_format(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&use_case=solar&format=json",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["T"] == [1.0, 2.0, None]  # NaN -> JSON null, not bare NaN
    assert len(body["index"]) == 3
    assert body["GHI"] == [0.0, 100.0, 200.0]


def test_weather_point_default_format_is_parquet(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018&use_case=solar",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"


def test_weather_point_requires_variables_or_use_case(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "Specify one of" in resp.get_json()["error"]


def test_weather_point_requires_api_key(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get("/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018")
    assert resp.status_code == 401


def test_weather_point_use_case_wind(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&use_case=wind&format=json",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"index", "WS_10M", "U_10M", "V_10M"}
    assert body["WS_10M"] == [3.0, 4.0, 5.0]


def test_weather_point_variables_subset(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&variables=WS_10M,T&format=json",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"index", "WS_10M", "T"}


def test_weather_point_unknown_use_case_is_400(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018&use_case=hydro",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "hydro" in resp.get_json()["error"]


def test_weather_point_both_variables_and_use_case_is_400(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&variables=T&use_case=solar",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "at most one" in resp.get_json()["error"]


def test_weather_variables_discovery_endpoint(client):
    resp = client.get("/v1/weather/variables", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["variables"]["WS_10M"]["unit"] == "m/s"
    assert body["use_cases"]["solar"] == ["T", "GHI", "DHI", "DNI"]
    assert body["use_cases"]["wind"] == ["WS_10M", "U_10M", "V_10M"]


def test_weather_variables_discovery_requires_api_key(client):
    resp = client.get("/v1/weather/variables")
    assert resp.status_code == 401


def test_health_is_liveness_only(client):
    resp = client.get("/v1/health", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_weather_providers_endpoint(client, monkeypatch):
    import weather.api.app as app_module
    import weather.registry

    monkeypatch.setattr(weather.registry, "list_providers", lambda: ["merra-2", "cosmo-rea6"])
    monkeypatch.setattr(
        app_module, "_available_years",
        lambda name: [2018] if name == "merra-2" else [],
    )
    resp = client.get("/v1/weather/providers", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["providers"]["merra-2"]["years"] == [2018]
    assert body["providers"]["cosmo-rea6"]["years"] == []


def test_weather_providers_requires_api_key(client):
    resp = client.get("/v1/weather/providers")
    assert resp.status_code == 401
