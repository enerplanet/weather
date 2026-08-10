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
    index = pd.date_range("2018-01-01", periods=3, freq="h", tz=None, name="time")
    return pd.DataFrame(
        {
            "T": [1.0, 2.0, np.nan],
            "GHI": [0.0, 100.0, 200.0],
            "DHI": [0.0, 50.0, 60.0],
            "DNI": [0.0, 300.0, 400.0],
        },
        index=index,
    )


def _patch_get_point_weather(monkeypatch):
    import weather

    monkeypatch.setattr(weather, "get_point_weather", lambda *a, **kw: _fake_weather_df())


def test_weather_point_json_format(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018&format=json",
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
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"


def test_weather_point_requires_api_key(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get("/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018")
    assert resp.status_code == 401
