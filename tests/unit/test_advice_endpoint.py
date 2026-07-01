"""`GET /v1/advice` — wiring the clients to the engine over HTTP.

Both upstreams are mocked with respx: OTP's GTFS GraphQL `plan` (POSTed once per mode
set, answered by the full requested mode list) and Buienradar's raintext. The clients are
swapped via FastAPI dependency overrides so the test points them at fake hosts. The bike
itinerary's epoch-ms times are anchored to the same fixed June day as the rain slots so
the engine sees them as aligned.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.api.deps import get_geocoder_client, get_otp_client, get_rain_service
from app.clients.buienradar import BuienradarClient
from app.clients.geocoder import GeocodeNotFound
from app.clients.otp import OTPClient
from app.core.cache import InMemoryCache
from app.main import app
from app.services.rain import RainService

TZ = ZoneInfo("Europe/Amsterdam")
OTP_URL = "http://otp.test"  # host root; client appends /otp/gtfs/v1
GQL_URL = f"{OTP_URL}/otp/gtfs/v1"
RAIN_URL = "https://rain.test/raintext"


def ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 6, 1, hour, minute, tzinfo=TZ).timestamp() * 1000)


def _gql(itineraries: list[dict]) -> dict:
    return {"data": {"plan": {"itineraries": itineraries}}}


BIKE_JSON = _gql(
    [
        {
            "duration": 1200,
            "startTime": ms(14, 0),
            "endTime": ms(14, 20),
            "legs": [
                {
                    "mode": "BICYCLE",
                    "startTime": ms(14, 0),
                    "endTime": ms(14, 20),
                    "duration": 1200,
                    "distance": 3000.0,
                    "route": None,
                }
            ],
        }
    ]
)

_TRANSIT_ITIN = {
    "duration": 720,
    "startTime": ms(14, 0),
    "endTime": ms(14, 12),
    "legs": [
        {
            "mode": "WALK",
            "startTime": ms(14, 0),
            "endTime": ms(14, 2),
            "duration": 120,
            "distance": 150.0,
            "route": None,
        },
        {
            "mode": "TRAM",
            "startTime": ms(14, 2),
            "endTime": ms(14, 12),
            "duration": 600,
            "distance": 2400.0,
            "route": {"shortName": "13"},
        },
    ],
}

# A WALK-only itinerary: OTP returns these as fallbacks in a TRANSIT,WALK plan when
# walking the whole way is feasible. It must NOT be mistaken for a transit option.
_WALK_ONLY_ITIN = {
    "duration": 2400,
    "startTime": ms(14, 0),
    "endTime": ms(14, 40),
    "legs": [
        {
            "mode": "WALK",
            "startTime": ms(14, 0),
            "endTime": ms(14, 40),
            "duration": 2400,
            "distance": 3000.0,
            "route": None,
        }
    ],
}

# A bike-and-ride itinerary the mixed query returns: bike to a stop, then tram.
_BIKE_RIDE_ITIN = {
    "duration": 900,
    "startTime": ms(14, 0),
    "endTime": ms(14, 15),
    "legs": [
        {
            "mode": "BICYCLE",
            "startTime": ms(14, 0),
            "endTime": ms(14, 5),
            "duration": 300,
            "distance": 1200.0,
            "route": None,
        },
        {
            "mode": "TRAM",
            "startTime": ms(14, 5),
            "endTime": ms(14, 15),
            "duration": 600,
            "distance": 2400.0,
            "route": {"shortName": "13"},
        },
    ],
}

TRANSIT_JSON = _gql([_TRANSIT_ITIN])
MIXED_JSON = _gql([_BIKE_RIDE_ITIN])

# OTP often orders a long WALK-only itinerary first; the real tram option comes second.
TRANSIT_JSON_WALK_FIRST = _gql([_WALK_ONLY_ITIN, _TRANSIT_ITIN])

# 109|14:05 -> 1.0 mm/h (wet); everything else dry.
RAIN_WET = "000|14:00\n109|14:05\n000|14:10\n000|14:20\n"
RAIN_DRY = "000|14:00\n000|14:05\n000|14:20\n"


class _FakeGeocoder:
    """Stand-in geocoder: resolves a couple of known names, raises otherwise."""

    _PLACES = {"amsterdam centraal": (52.3791, 4.9003), "vondelpark": (52.3579, 4.8686)}

    async def geocode(self, query: str) -> tuple[float, float]:
        key = query.strip().lower()
        if key not in self._PLACES:
            raise GeocodeNotFound(query)
        return self._PLACES[key]


def _rain_service() -> RainService:
    # Real rain service over a fresh in-memory cache, pointed at the mocked Buienradar.
    # Exercises the actual caching + degradation path instead of stubbing it out.
    return RainService(
        BuienradarClient(base_url=RAIN_URL),
        InMemoryCache(),
        fresh_seconds=300,
        retention_seconds=7200,
    )


@pytest.fixture(autouse=True)
def _override_clients():
    app.dependency_overrides[get_otp_client] = lambda: OTPClient(base_url=OTP_URL)
    app.dependency_overrides[get_rain_service] = _rain_service
    app.dependency_overrides[get_geocoder_client] = _FakeGeocoder
    yield
    app.dependency_overrides.clear()


def _otp_by_modes(by_key: dict[str, httpx.Response]):
    """respx side_effect answering by the full requested mode set (comma-joined)."""

    def handler(request: httpx.Request) -> httpx.Response:
        modes = json.loads(request.content)["variables"]["modes"]
        key = ",".join(m["mode"] for m in modes)
        return by_key.get(key, httpx.Response(200, json=_gql([])))

    return handler


@respx.mock
def test_rain_during_ride_returns_transit():
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_WET))

    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "transit"
    assert body["rain_expected"] is True
    assert "tram 13" in body["reason"].lower()


@respx.mock
def test_walk_only_itinerary_first_is_skipped_for_real_transit():
    # OTP lists a long WALK-only itinerary before the tram. The endpoint must pick the
    # real transit option, not the walk: a walk leaves the rider just as wet as cycling.
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON_WALK_FIRST),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_WET))

    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "transit"
    # 12-min tram, not the 40-min walk-only fallback.
    assert body["transit_minutes"] == 12
    assert "tram 13" in body["reason"].lower()


@respx.mock
def test_walk_only_transit_plan_falls_back_to_bike():
    # If the ONLY transit answer is walking, there is no real transit option: a dry-day
    # ride should recommend bike with transit_minutes None, same as no transit at all.
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=_gql([_WALK_ONLY_ITIN])),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=_gql([])),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))

    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "bike"
    assert body["transit_minutes"] is None


@respx.mock
def test_place_names_are_geocoded():
    # The whole point: a rider types names, not coordinates, and still gets advice.
    # With transit at 12 min and bike at 20 min on a dry day, transit wins on raw cost.
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))

    response = TestClient(app).get(
        "/v1/advice", params={"from": "Amsterdam Centraal", "to": "Vondelpark"}
    )

    assert response.status_code == 200
    # Dry day: bike=20, transit=12+10 bias=22, mixed=15+10=25 -> bike wins.
    assert response.json()["recommendation"] == "bike"


@respx.mock
def test_unknown_place_name_returns_400():
    # A name the geocoder can't resolve is a client input problem, not an upstream error.
    response = TestClient(app).get("/v1/advice", params={"from": "Atlantis", "to": "Vondelpark"})

    assert response.status_code == 400


@respx.mock
def test_geocoder_upstream_failure_returns_502():
    class _BrokenGeocoder:
        async def geocode(self, query: str) -> tuple[float, float]:
            raise httpx.ConnectError("nominatim down")

    app.dependency_overrides[get_geocoder_client] = _BrokenGeocoder

    response = TestClient(app).get(
        "/v1/advice", params={"from": "Amsterdam Centraal", "to": "Vondelpark"}
    )

    assert response.status_code == 502


@respx.mock
def test_invalid_coordinates_returns_400():
    # A value that is neither valid coordinates nor a resolvable name still 400s.
    response = TestClient(app).get("/v1/advice", params={"from": "12,34,56", "to": "52.35,4.86"})

    assert response.status_code == 400


@respx.mock
def test_transit_unavailable_still_returns_bike():
    # OTP can route a bike but has no transit answer; the endpoint must still respond.
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(503),
                "BICYCLE,TRANSIT,WALK": httpx.Response(503),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))

    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "bike"
    assert body["transit_minutes"] is None


@respx.mock
def test_buienradar_down_degrades_to_bike():
    # One flaky upstream must not take down the recommendation: with Buienradar failing
    # and nothing cached, the endpoint still answers with rain forecast flagged unknown.
    # Without rain data, bias kicks in: bike=20, transit=12+10=22 -> bike wins.
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(503))

    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "bike"
    assert body["rain_expected"] is None
    assert body["max_rain_mm_per_h"] is None
    assert "unavailable" in body["reason"].lower()


@respx.mock
def test_bike_routing_failure_returns_502():
    respx.post(GQL_URL).mock(return_value=httpx.Response(503))
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))

    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 502


@respx.mock
def test_advice_no_bike_route_reports_null_bike_minutes():
    # OTP finds no pure-bike route; advice must report bike_minutes=None, not a transit duration.
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=_gql([])),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=_gql([])),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))
    response = TestClient(app).get("/v1/advice", params={"from": "52.37,4.89", "to": "52.35,4.86"})
    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "transit"
    assert body["bike_minutes"] is None
    assert body["transit_minutes"] == 12
