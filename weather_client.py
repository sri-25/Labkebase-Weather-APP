"""
Client for the National Weather Service (NWS) API (api.weather.gov).

No API key required - NWS just asks for a descriptive User-Agent header
identifying the app/contact so they can reach out if a client misbehaves
(see https://www.weather.gov/documentation/services-web-api). This mirrors
massive_client.py's shape (a thin requests.Session wrapper) but has no
secret-scope auth step at all, since there's nothing to authenticate.

Two-layer location resolution, since NWS takes lat/lon but the sync
endpoint accepts human-friendly "City, ST" strings:
  1. A small static dict of known cities (fast, deterministic, no network -
     keeps unit tests hermetic).
  2. Fallback to OpenStreetMap's Nominatim geocoder - free, no key, no
     signup - for any city/state not in the dict.
  3. Raw "lat,lon" strings bypass geocoding entirely.
This means adding a new city later is just passing its name in the
/weather/sync request body - no code change required.

Note on the fallback choice: the US Census Bureau's geocoder was tried
first, since it's a well-known free/no-key option - but it turned out to
only match specific STREET ADDRESSES against TIGER address ranges
("1600 Pennsylvania Ave, Washington, DC" works; a bare "Denver, CO" does
not, no matter how well-known the city is). Nominatim is built for
place/city-level queries directly, which is what this actually needs.
Subject to Nominatim's usage policy (max ~1 request/sec, requires an
identifying User-Agent - reuses DEFAULT_USER_AGENT below) - fine for this
project's interactive, one-city-at-a-time watchlist-add use case.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

NWS_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")

# NWS asks for a descriptive User-Agent so they can contact the operator of
# a misbehaving client - a generic UA (or none) can get you 403'd.
DEFAULT_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT", "(weather-intelligence-homework, srijan2554@gmail.com)"
)

_DEFAULT_TIMEOUT = 30

# Fast-path static lookup for the V1 seed cities - avoids a network round
# trip to the Census geocoder for the locations we test against most.
# Values are (lat, lon). Add more here as convenient, but nothing requires
# it - the Census geocoder fallback below handles any city not listed.
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "chicago, il": (41.8781, -87.6298),
    "austin, tx": (30.2672, -97.7431),
    "miami, fl": (25.7617, -80.1918),
}

# Matches raw "lat,lon" input, e.g. "41.8781,-87.6298" or "41.8781, -87.6298"
_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class GeocodeError(Exception):
    """Raised when a location string can't be resolved to lat/lon."""


class WeatherClient:
    """Thin wrapper around the NWS API. No auth required."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        user_agent: str | None = None,
    ):
        self.base_url = (base_url or NWS_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept": "application/geo+json",
            }
        )

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def geocode(self, location: str) -> tuple[float, float]:
        """
        Resolve a location string to (lat, lon).

        Accepts, in order of precedence:
          1. Raw "lat,lon" (bypasses geocoding entirely)
          2. A known city in the static _CITY_COORDS dict
          3. Any other "City, ST"-style string, via the Census geocoder

        Raises GeocodeError if none of the above resolve.
        """
        location = (location or "").strip()
        if not location:
            raise GeocodeError("Empty location string")

        m = _LATLON_RE.match(location)
        if m:
            return float(m.group(1)), float(m.group(2))

        key = location.lower()
        if key in _CITY_COORDS:
            return _CITY_COORDS[key]

        geocoded = self._geocode_nominatim(location)
        if geocoded is not None:
            return geocoded

        raise GeocodeError(
            f"Could not resolve {location!r}. Check the spelling, or use "
            "\"City, ST\" format (e.g. \"Denver, CO\")."
        )

    def _geocode_nominatim(self, location: str) -> tuple[float, float] | None:
        """
        Single call to OpenStreetMap's Nominatim geocoder (free, no key).
        Returns (lat, lon) or None if no match.

        Response shape (format=json) is a JSON ARRAY, not a dict:
          [{"lat": "39.739...", "lon": "-104.990...", ...}, ...]
        Nominatim requires an identifying User-Agent per its usage policy -
        reuses this client's session header rather than a default library UA.
        """
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": self._session.headers["User-Agent"]},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        matches = resp.json()
        if not matches:
            return None
        return float(matches[0]["lat"]), float(matches[0]["lon"])

    # ------------------------------------------------------------------
    # NWS API calls
    # ------------------------------------------------------------------

    def get_point_metadata(self, lat: float, lon: float) -> dict[str, Any]:
        """
        GET /points/{lat},{lon} - resolves a coordinate to its forecast
        office + grid cell (gridId, gridX, gridY), which every other NWS
        endpoint needs.
        """
        resp = self._session.get(
            f"{self.base_url}/points/{lat},{lon}", timeout=self.timeout
        )
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        rel_location = props.get("relativeLocation", {}).get("properties", {})
        return {
            "grid_id": props.get("gridId"),
            "grid_x": props.get("gridX"),
            "grid_y": props.get("gridY"),
            "forecast_url": props.get("forecast"),
            "forecast_hourly_url": props.get("forecastHourly"),
            "city": rel_location.get("city"),
            "state": rel_location.get("state"),
        }

    def get_active_alerts(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """
        GET /alerts/active?point={lat},{lon} - active alerts covering this
        point. Returns the raw GeoJSON "features" list (one per alert).
        """
        resp = self._session.get(
            f"{self.base_url}/alerts/active",
            params={"point": f"{lat},{lon}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("features", [])

    def get_forecast_periods(self, grid_id: str, grid_x: int, grid_y: int) -> dict[str, Any]:
        """
        GET /gridpoints/{office}/{x},{y}/forecast - the narrative multi-day
        forecast (NOT the raw numeric gridpoint data). Returns
        {"updated": <ISO timestamp>, "periods": [...]}.
        """
        resp = self._session.get(
            f"{self.base_url}/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        return {"updated": props.get("updated"), "periods": props.get("periods", [])}

    # ------------------------------------------------------------------
    # Normalization -> document schema
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_alert(feature: dict[str, Any], location_label: str) -> dict[str, Any]:
        """
        Normalize one NWS alert GeoJSON feature into the document schema.

        id: the alert's own stable NWS identifier (a URN, e.g.
        "urn:oid:2.49.0.1.840.0....") - NWS alerts already have a globally
        stable ID, so no synthesized key is needed here.

        narrative_text: description + instruction combined - the
        instruction field ("Turn around, don't drown...") is often where
        the actionable guidance lives, and combining them gives richer
        embeddings than description alone.
        """
        props = feature.get("properties", {}) or {}
        description = (props.get("description") or "").strip()
        instruction = (props.get("instruction") or "").strip()
        narrative_text = "\n\n".join(t for t in (description, instruction) if t)

        return {
            "id": props.get("id") or feature.get("id"),
            "location": location_label,
            "source_type": "alert",
            "headline": props.get("event") or props.get("headline"),
            "narrative_text": narrative_text,
            "issued_at": props.get("effective") or props.get("onset") or props.get("sent"),
            "effective_at": props.get("expires") or props.get("ends"),
            "payload": feature,
        }

    @staticmethod
    def normalize_forecast_period(
        period: dict[str, Any],
        location_label: str,
        grid_id: str,
        grid_x: int,
        grid_y: int,
        updated: str | None,
    ) -> dict[str, Any]:
        """
        Normalize one forecast period (e.g. "Tonight", "Monday") into the
        document schema.

        id: forecast periods have no natural stable ID from NWS, so we
        synthesize one from grid cell + period number + startTime. This is
        stable across re-syncs within the same forecast cycle (so ON
        CONFLICT upserts correctly instead of duplicating), but changes
        once NWS regenerates the forecast package (~twice daily) - old
        period rows are left in place rather than deleted. See
        README_WEATHER.md limitations for the cleanup-job follow-up.
        """
        period_number = period.get("number")
        start_time = period.get("startTime")
        doc_id = f"{grid_id}-{grid_x}-{grid_y}-fc-{period_number}-{start_time}"

        return {
            "id": doc_id,
            "location": location_label,
            "source_type": "forecast",
            "headline": period.get("name"),
            "narrative_text": period.get("detailedForecast") or "",
            "issued_at": updated,
            "effective_at": start_time,
            "payload": period,
        }

    # ------------------------------------------------------------------
    # High-level per-location sync
    # ------------------------------------------------------------------

    def sync_location(self, location_label: str, limit: int = 50) -> list[dict[str, Any]]:
        """
        Full harvest for one location: geocode -> resolve grid point ->
        fetch active alerts + forecast periods -> normalize both into the
        document schema. Returns a combined, capped list (alerts first,
        since they're time-sensitive/actionable; forecast periods fill the
        remainder of `limit`).
        """
        lat, lon = self.geocode(location_label)
        point = self.get_point_metadata(lat, lon)

        alert_features = self.get_active_alerts(lat, lon)
        alert_docs = [self.normalize_alert(f, location_label) for f in alert_features]

        forecast_docs: list[dict[str, Any]] = []
        if point.get("grid_id") is not None:
            forecast = self.get_forecast_periods(
                point["grid_id"], point["grid_x"], point["grid_y"]
            )
            forecast_docs = [
                self.normalize_forecast_period(
                    p,
                    location_label,
                    point["grid_id"],
                    point["grid_x"],
                    point["grid_y"],
                    forecast["updated"],
                )
                for p in forecast["periods"]
            ]

        documents = alert_docs + forecast_docs
        return documents[:limit] if limit else documents


def utcnow_iso() -> str:
    """Helper for callers that want a synced_at timestamp client-side."""
    return datetime.now(timezone.utc).isoformat()
