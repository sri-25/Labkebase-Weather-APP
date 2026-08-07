"""
Unit tests for weather_client.py. Fully hermetic - no real network calls.
HTTP is mocked with `responses`, so these run the same in CI, locally, and
in a sandbox with no route to api.weather.gov.

Run: pytest test_weather_client.py -v
"""

import responses

from weather_client import GeocodeError, WeatherClient


# ----------------------------------------------------------------------
# Geocoding
# ----------------------------------------------------------------------

def test_geocode_raw_latlon_bypasses_network():
    client = WeatherClient()
    lat, lon = client.geocode("41.8781,-87.6298")
    assert lat == 41.8781
    assert lon == -87.6298


def test_geocode_raw_latlon_with_spaces():
    client = WeatherClient()
    lat, lon = client.geocode(" 30.2672, -97.7431 ")
    assert lat == 30.2672
    assert lon == -97.7431


def test_geocode_known_city_static_dict_case_insensitive():
    client = WeatherClient()
    lat, lon = client.geocode("Chicago, IL")
    assert (lat, lon) == (41.8781, -87.6298)

    lat, lon = client.geocode("chicago, il")
    assert (lat, lon) == (41.8781, -87.6298)


@responses.activate
def test_geocode_unknown_city_falls_back_to_nominatim():
    responses.add(
        responses.GET,
        "https://nominatim.openstreetmap.org/search",
        json=[{"lat": "37.7749", "lon": "-122.4194"}],
        status=200,
    )
    client = WeatherClient()
    lat, lon = client.geocode("San Francisco, CA")
    assert lat == 37.7749
    assert lon == -122.4194


@responses.activate
def test_geocode_unknown_city_no_nominatim_match_raises():
    responses.add(
        responses.GET,
        "https://nominatim.openstreetmap.org/search",
        json=[],
        status=200,
    )
    client = WeatherClient()
    try:
        client.geocode("Nowhereville, ZZ")
        assert False, "expected GeocodeError"
    except GeocodeError:
        pass


def test_geocode_empty_string_raises():
    client = WeatherClient()
    try:
        client.geocode("   ")
        assert False, "expected GeocodeError"
    except GeocodeError:
        pass


# ----------------------------------------------------------------------
# NWS API calls
# ----------------------------------------------------------------------

@responses.activate
def test_get_point_metadata_parses_grid_and_urls():
    responses.add(
        responses.GET,
        "https://api.weather.gov/points/41.8781,-87.6298",
        json={
            "properties": {
                "gridId": "LOT",
                "gridX": 70,
                "gridY": 72,
                "forecast": "https://api.weather.gov/gridpoints/LOT/70,72/forecast",
                "forecastHourly": "https://api.weather.gov/gridpoints/LOT/70,72/forecast/hourly",
                "relativeLocation": {
                    "properties": {"city": "Chicago", "state": "IL"}
                },
            }
        },
        status=200,
    )
    client = WeatherClient()
    point = client.get_point_metadata(41.8781, -87.6298)
    assert point["grid_id"] == "LOT"
    assert point["grid_x"] == 70
    assert point["grid_y"] == 72
    assert point["city"] == "Chicago"
    assert point["state"] == "IL"


@responses.activate
def test_get_active_alerts_returns_features():
    responses.add(
        responses.GET,
        "https://api.weather.gov/alerts/active",
        json={"features": [{"id": "urn:oid:1", "properties": {"event": "Flash Flood Warning"}}]},
        status=200,
    )
    client = WeatherClient()
    features = client.get_active_alerts(41.8781, -87.6298)
    assert len(features) == 1
    assert features[0]["properties"]["event"] == "Flash Flood Warning"


@responses.activate
def test_get_active_alerts_empty_list_when_none_active():
    responses.add(
        responses.GET,
        "https://api.weather.gov/alerts/active",
        json={"features": []},
        status=200,
    )
    client = WeatherClient()
    assert client.get_active_alerts(41.8781, -87.6298) == []


@responses.activate
def test_get_forecast_periods_returns_periods_and_updated():
    responses.add(
        responses.GET,
        "https://api.weather.gov/gridpoints/LOT/70,72/forecast",
        json={
            "properties": {
                "updated": "2026-08-06T12:00:00+00:00",
                "periods": [
                    {
                        "number": 1,
                        "name": "Tonight",
                        "startTime": "2026-08-06T18:00:00-05:00",
                        "detailedForecast": "Mostly clear, with a low around 68.",
                    }
                ],
            }
        },
        status=200,
    )
    client = WeatherClient()
    result = client.get_forecast_periods("LOT", 70, 72)
    assert result["updated"] == "2026-08-06T12:00:00+00:00"
    assert len(result["periods"]) == 1
    assert result["periods"][0]["name"] == "Tonight"


# ----------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------

def test_normalize_alert_combines_description_and_instruction():
    feature = {
        "id": "https://api.weather.gov/alerts/urn:oid:1",
        "properties": {
            "id": "urn:oid:1",
            "event": "Flash Flood Warning",
            "description": "At 300 PM, heavy rain was reported.",
            "instruction": "Turn around, don't drown.",
            "effective": "2026-08-06T15:00:00-05:00",
            "expires": "2026-08-06T18:00:00-05:00",
        },
    }
    doc = WeatherClient.normalize_alert(feature, "Chicago, IL")
    assert doc["id"] == "urn:oid:1"
    assert doc["source_type"] == "alert"
    assert doc["headline"] == "Flash Flood Warning"
    assert "heavy rain was reported" in doc["narrative_text"]
    assert "Turn around, don't drown" in doc["narrative_text"]
    assert doc["issued_at"] == "2026-08-06T15:00:00-05:00"
    assert doc["effective_at"] == "2026-08-06T18:00:00-05:00"
    assert doc["payload"] == feature


def test_normalize_alert_missing_instruction_uses_description_only():
    feature = {
        "properties": {
            "id": "urn:oid:2",
            "event": "Heat Advisory",
            "description": "Heat index values up to 105.",
            "instruction": None,
            "effective": "2026-08-06T10:00:00-05:00",
        }
    }
    doc = WeatherClient.normalize_alert(feature, "Austin, TX")
    assert doc["narrative_text"] == "Heat index values up to 105."


def test_normalize_alert_falls_back_to_feature_id_when_no_properties_id():
    feature = {"id": "https://api.weather.gov/alerts/urn:oid:3", "properties": {}}
    doc = WeatherClient.normalize_alert(feature, "Miami, FL")
    assert doc["id"] == "https://api.weather.gov/alerts/urn:oid:3"


def test_normalize_forecast_period_builds_stable_id_and_narrative():
    period = {
        "number": 1,
        "name": "Tonight",
        "startTime": "2026-08-06T18:00:00-05:00",
        "detailedForecast": "Mostly clear, with a low around 68.",
    }
    doc = WeatherClient.normalize_forecast_period(
        period, "Chicago, IL", "LOT", 70, 72, "2026-08-06T12:00:00+00:00"
    )
    assert doc["id"] == "LOT-70-72-fc-1-2026-08-06T18:00:00-05:00"
    assert doc["source_type"] == "forecast"
    assert doc["headline"] == "Tonight"
    assert doc["narrative_text"] == "Mostly clear, with a low around 68."
    assert doc["issued_at"] == "2026-08-06T12:00:00+00:00"
    assert doc["effective_at"] == "2026-08-06T18:00:00-05:00"


def test_normalize_forecast_period_id_stable_across_reruns_same_cycle():
    """Same period fetched twice in the same forecast cycle -> same id, so
    an upsert (ON CONFLICT) updates rather than duplicates."""
    period = {"number": 1, "name": "Tonight", "startTime": "2026-08-06T18:00:00-05:00"}
    doc1 = WeatherClient.normalize_forecast_period(period, "Chicago, IL", "LOT", 70, 72, "t1")
    doc2 = WeatherClient.normalize_forecast_period(period, "Chicago, IL", "LOT", 70, 72, "t2")
    assert doc1["id"] == doc2["id"]


# ----------------------------------------------------------------------
# sync_location (end-to-end with mocked HTTP)
# ----------------------------------------------------------------------

@responses.activate
def test_sync_location_combines_alerts_and_forecast_alerts_first():
    responses.add(
        responses.GET,
        "https://api.weather.gov/points/41.8781,-87.6298",
        json={
            "properties": {
                "gridId": "LOT",
                "gridX": 70,
                "gridY": 72,
                "relativeLocation": {"properties": {"city": "Chicago", "state": "IL"}},
            }
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.weather.gov/alerts/active",
        json={
            "features": [
                {"properties": {"id": "urn:oid:1", "event": "Flash Flood Warning", "description": "d"}}
            ]
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.weather.gov/gridpoints/LOT/70,72/forecast",
        json={
            "properties": {
                "updated": "t",
                "periods": [
                    {"number": 1, "name": "Tonight", "startTime": "s1", "detailedForecast": "f1"},
                    {"number": 2, "name": "Monday", "startTime": "s2", "detailedForecast": "f2"},
                ],
            }
        },
        status=200,
    )

    client = WeatherClient()
    docs = client.sync_location("Chicago, IL", limit=50)

    assert len(docs) == 3
    assert docs[0]["source_type"] == "alert"
    assert docs[1]["source_type"] == "forecast"
    assert docs[2]["source_type"] == "forecast"


@responses.activate
def test_sync_location_respects_limit_cap():
    responses.add(
        responses.GET,
        "https://api.weather.gov/points/41.8781,-87.6298",
        json={"properties": {"gridId": "LOT", "gridX": 70, "gridY": 72}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.weather.gov/alerts/active",
        json={"features": [{"properties": {"id": f"urn:oid:{i}", "event": "E", "description": "d"}} for i in range(5)]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.weather.gov/gridpoints/LOT/70,72/forecast",
        json={"properties": {"updated": "t", "periods": [{"number": i, "name": f"P{i}", "startTime": f"s{i}"} for i in range(5)]}},
        status=200,
    )

    client = WeatherClient()
    docs = client.sync_location("Chicago, IL", limit=3)
    assert len(docs) == 3


@responses.activate
def test_sync_location_no_grid_id_skips_forecast_gracefully():
    """Edge case: /points resolves but returns no gridId (e.g. a point
    outside NWS coverage) - forecast fetch should be skipped, not crash."""
    responses.add(
        responses.GET,
        "https://api.weather.gov/points/1.0,1.0",
        json={"properties": {}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.weather.gov/alerts/active",
        json={"features": []},
        status=200,
    )

    client = WeatherClient()
    docs = client.sync_location("1.0,1.0", limit=50)
    assert docs == []
