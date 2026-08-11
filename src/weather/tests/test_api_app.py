"""Unit tests for the ``weather serve`` Flask app (src/weather/api/app.py).

``weather.get_point_weather`` is monkeypatched -- these exercise the HTTP
layer (auth, format switch, NaN handling), not the point-query pipeline
itself (see test_point_query.py for that).

Run with::

    conda run -n weather_env pytest src/weather/tests/test_api_app.py
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

flask = pytest.importorskip("flask")

from weather import errors  # noqa: E402
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
    assert set(body) == {"index", "variables"}
    variables = body["variables"]
    assert variables["T"] == [1.0, 2.0, None]  # NaN -> JSON null, not bare NaN
    assert len(body["index"]) == 3
    assert len(variables["T"]) == len(body["index"])
    assert variables["GHI"] == [0.0, 100.0, 200.0]


def test_weather_point_json_timestamps_are_utc_rfc3339(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&use_case=solar&format=json",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    for ts in resp.get_json()["index"]:
        assert ts.endswith("Z")
        datetime.fromisoformat(ts)  # raises if not a valid RFC3339 timestamp


def test_weather_point_default_format_is_parquet(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018&use_case=solar",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"


def test_weather_point_rate_limit_headers_present_on_200(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018&use_case=solar",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.headers["RateLimit-Limit"] == "60"
    assert int(resp.headers["RateLimit-Remaining"]) == 59
    assert int(resp.headers["RateLimit-Reset"]) > 0
    assert "Retry-After" not in resp.headers


def test_weather_point_requires_api_key(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get("/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018")
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == errors.INVALID_API_KEY


def test_weather_point_use_case_wind(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&use_case=wind&format=json",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"index", "variables"}
    assert set(body["variables"]) == {"WS_10M", "U_10M", "V_10M"}
    assert body["variables"]["WS_10M"] == [3.0, 4.0, 5.0]


def test_weather_point_variables_subset(client, monkeypatch):
    _patch_get_point_weather(monkeypatch)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018"
        "&variables=WS_10M,T&format=json",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body["variables"]) == {"WS_10M", "T"}


# One entry per distinct 400 cause the reviewer's brief requires a
# dedicated code for -- query string plus the code the response must
# carry. Message text is deliberately not asserted (see #13): a caller
# is meant to branch on `code`, and asserting message text here would
# just make this test as brittle as the string-matching this design
# exists to avoid.
_BAD_REQUEST_CASES = [
    pytest.param(
        "provider=merra-2&lon=5.0&year=2018",  # lat missing
        errors.MISSING_PARAMETER,
        id="missing_parameter",
    ),
    pytest.param(
        "provider=merra-2&lat=not-a-number&lon=5.0&year=2018",
        errors.NON_NUMERIC_PARAMETER,
        id="non_numeric_parameter",
    ),
    pytest.param(
        "provider=merra-2&lat=91&lon=5.0&year=2018",
        errors.COORDINATE_OUT_OF_RANGE,
        id="latitude_out_of_range",
    ),
    pytest.param(
        "provider=merra-2&lat=52.0&lon=-181&year=2018",
        errors.COORDINATE_OUT_OF_RANGE,
        id="longitude_out_of_range",
    ),
    pytest.param(
        "lat=52.0&lon=5.0&year=2018&use_case=solar",  # provider missing
        errors.MISSING_PARAMETER,
        id="missing_provider",
    ),
    pytest.param(
        "provider=not-a-real-provider&lat=52.0&lon=5.0&year=2018&use_case=solar",
        errors.UNKNOWN_PROVIDER,
        id="unknown_provider",
    ),
    pytest.param(
        "provider=merra-2&lat=52.0&lon=5.0&year=2018&variables=NOT_A_VARIABLE",
        errors.UNKNOWN_VARIABLE,
        id="unknown_variable",
    ),
    pytest.param(
        "provider=merra-2&lat=52.0&lon=5.0&year=2018&use_case=hydro",
        errors.UNKNOWN_USE_CASE,
        id="unknown_use_case",
    ),
    pytest.param(
        "provider=merra-2&lat=52.0&lon=5.0&year=2018",  # neither given
        errors.VARIABLES_USE_CASE_REQUIRED,
        id="variables_use_case_required",
    ),
    pytest.param(
        "provider=merra-2&lat=52.0&lon=5.0&year=2018&variables=T&use_case=solar",
        errors.VARIABLES_USE_CASE_CONFLICT,
        id="variables_use_case_conflict",
    ),
]


@pytest.mark.parametrize("query, expected_code", _BAD_REQUEST_CASES)
def test_weather_point_400_causes(client, query, expected_code):
    # No _patch_get_point_weather here: every case except unknown_provider
    # fails inside the handler itself (param parsing, range check, or
    # resolve_variables), before get_point_weather is ever called --
    # unknown_provider specifically needs the REAL get_point_weather, since
    # that's where provider validation actually lives, and it raises
    # before touching any archive/filesystem, so this is safe.
    resp = client.get(f"/v1/weather/point?{query}", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == expected_code


def test_weather_point_archive_not_found_is_404(client, monkeypatch):
    import weather

    def _raise(*args, **kwargs):
        raise FileNotFoundError("no processed archive for merra-2/2018")

    monkeypatch.setattr(weather, "get_point_weather", _raise)
    # A lat/lon/year combination not used by any other test in this file --
    # _cached_point_weather's lru_cache is keyed on (provider, lat, lon,
    # year, variables) and persists across tests in the same process, so
    # reusing another test's key would silently return its cached success
    # instead of ever calling this test's raising fake.
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=10.0&lon=10.0&year=2001&use_case=solar",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == errors.ARCHIVE_NOT_FOUND


def test_weather_point_unservable_archive_is_503(client, monkeypatch):
    import weather

    def _raise(*args, **kwargs):
        raise RuntimeError("unrepaired ERA5-Land boundary month")

    monkeypatch.setattr(weather, "get_point_weather", _raise)
    resp = client.get(
        "/v1/weather/point?provider=merra-2&lat=20.0&lon=20.0&year=2002&use_case=solar",
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 503
    assert resp.get_json()["error"]["code"] == errors.SERVICE_UNAVAILABLE


def test_weather_point_rate_limited_is_429(monkeypatch):
    _patch_get_point_weather(monkeypatch)
    monkeypatch.setenv("WEATHER_API_KEYS", API_KEY)
    monkeypatch.setenv("WEATHER_API_RATE_LIMIT", "1")
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as limited_client:
        url = "/v1/weather/point?provider=merra-2&lat=52.0&lon=5.0&year=2018&use_case=solar"
        first = limited_client.get(url, headers={"X-API-Key": API_KEY})
        assert first.status_code == 200
        second = limited_client.get(url, headers={"X-API-Key": API_KEY})
        assert second.status_code == 429
        assert second.get_json()["error"]["code"] == errors.RATE_LIMIT_EXCEEDED
        assert int(second.headers["Retry-After"]) > 0
        assert second.headers["RateLimit-Remaining"] == "0"


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


def test_weather_providers_per_item_error_uses_shared_error_shape(client, monkeypatch):
    """A per-provider failure inside the 200 body uses the same
    {code, message, details?} object as every other error response --
    one shape to detect a failure with, regardless of where it appears.
    """
    import weather.api.app as app_module
    import weather.registry

    monkeypatch.setattr(weather.registry, "list_providers", lambda: ["merra-2"])

    def _raise(name):
        raise OSError("archive directory not readable")

    monkeypatch.setattr(app_module, "_available_years", _raise)
    resp = client.get("/v1/weather/providers", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    error = resp.get_json()["providers"]["merra-2"]["error"]
    assert error["code"] == errors.PROVIDER_LISTING_FAILED
    assert "message" in error
