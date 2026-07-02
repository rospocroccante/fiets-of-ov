"""`GET /v1/plan` — the rich, drawable counterpart to `/v1/advice`.

Same wiring as the advice endpoint (OTP + Buienradar mocked with respx, clients swapped via
dependency overrides), but the leg fixtures carry the extra detail `/v1/plan` exposes --
`from`/`to` places, `legGeometry`, route long name, headsign -- so the test asserts the full
drawable shape, not just the recommendation.
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
from app.clients.otp import BIKE_MODES, OTPClient
from app.core.cache import InMemoryCache
from app.main import app
from app.services.rain import RainService

TZ = ZoneInfo("Europe/Amsterdam")
OTP_URL = "http://otp.test"
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
                    "from": {"name": "Origin", "lat": 52.37, "lon": 4.89},
                    "to": {"name": "Destination", "lat": 52.35, "lon": 4.86},
                    "legGeometry": {"points": "_p~iF~ps|U_ulLnnqC"},
                    "steps": [
                        {"distance": 18.0, "relativeDirection": "DEPART", "streetName": "Damrak"}
                    ],
                }
            ],
        }
    ]
)

TRANSIT_JSON = _gql(
    [
        {
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
                    "from": {"name": "Origin", "lat": 52.37, "lon": 4.89},
                    "to": {"name": "Dam", "lat": 52.373, "lon": 4.892},
                    "legGeometry": {"points": "_p~iF~ps|U"},
                },
                {
                    "mode": "TRAM",
                    "startTime": ms(14, 2),
                    "endTime": ms(14, 12),
                    "duration": 600,
                    "distance": 2400.0,
                    "route": {"shortName": "13", "longName": "Tram 13"},
                    "trip": {"tripHeadsign": "Geuzenveld"},
                    "from": {"name": "Dam", "lat": 52.373, "lon": 4.892},
                    "to": {"name": "Vondelpark", "lat": 52.358, "lon": 4.868},
                    "legGeometry": {"points": "_ulLnnqC_mqNvxq`@"},
                },
            ],
        }
    ]
)

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
            "from": {"name": "Origin", "lat": 52.37, "lon": 4.89},
            "to": {"name": "Dam", "lat": 52.373, "lon": 4.892},
            "legGeometry": {"points": "_p~iF~ps|U"},
            "steps": [],
        },
        {
            "mode": "TRAM",
            "startTime": ms(14, 5),
            "endTime": ms(14, 15),
            "duration": 600,
            "distance": 2400.0,
            "route": {"shortName": "13", "longName": "Tram 13"},
            "trip": {"tripHeadsign": "Geuzenveld"},
            "from": {"name": "Dam", "lat": 52.373, "lon": 4.892},
            "to": {"name": "Vondelpark", "lat": 52.358, "lon": 4.868},
            "legGeometry": {"points": "_ulLnnqC_mqNvxq`@"},
        },
    ],
}

MIXED_JSON = _gql([_BIKE_RIDE_ITIN])

RAIN_WET = "000|14:00\n109|14:05\n000|14:10\n000|14:20\n"
RAIN_DRY = "000|14:00\n000|14:05\n000|14:20\n"


class _FakeGeocoder:
    _PLACES = {"amsterdam centraal": (52.3791, 4.9003), "vondelpark": (52.3579, 4.8686)}

    async def geocode(self, query: str) -> tuple[float, float]:
        key = query.strip().lower()
        if key not in self._PLACES:
            raise GeocodeNotFound(query)
        return self._PLACES[key]


def _rain_service() -> RainService:
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
def test_plan_returns_both_itineraries_with_geometry_and_legs():
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                BIKE_MODES: httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_WET))

    response = TestClient(app).get("/v1/plan", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "transit"

    options = body["options"]
    kinds = {o["kind"] for o in options}
    assert "bike" in kinds

    bike_option = next(o for o in options if o["kind"] == "bike")
    assert bike_option["itinerary"]["minutes"] == 20
    assert bike_option["itinerary"]["legs"][0]["geometry"]  # encoded polyline present
    assert bike_option["itinerary"]["legs"][0]["steps"][0]["street"] == "Damrak"

    transit_option = next((o for o in options if o["kind"] == "transit"), None)
    assert transit_option is not None
    modes = [leg["mode"] for leg in transit_option["itinerary"]["legs"]]
    assert modes == ["WALK", "TRAM"]
    tram = transit_option["itinerary"]["legs"][1]
    assert tram["route"] == "13"
    assert tram["headsign"] == "Geuzenveld"
    assert tram["from"]["name"] == "Dam"
    assert tram["to"]["name"] == "Vondelpark"
    assert tram["geometry"]


@respx.mock
def test_plan_transit_unavailable_returns_bike_only():
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                BIKE_MODES: httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(503),
                "BICYCLE,TRANSIT,WALK": httpx.Response(503),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))

    response = TestClient(app).get("/v1/plan", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "bike"
    options = body["options"]
    assert len(options) == 1
    assert options[0]["kind"] == "bike"
    assert options[0]["itinerary"]["legs"][0]["mode"] == "BICYCLE"


@respx.mock
def test_plan_unknown_place_returns_400():
    response = TestClient(app).get("/v1/plan", params={"from": "Atlantis", "to": "Vondelpark"})
    assert response.status_code == 400


@respx.mock
def test_plan_bike_routing_failure_returns_502():
    respx.post(GQL_URL).mock(return_value=httpx.Response(503))
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_DRY))

    response = TestClient(app).get("/v1/plan", params={"from": "52.37,4.89", "to": "52.35,4.86"})
    assert response.status_code == 502


@respx.mock
def test_plan_returns_ranked_options_with_bike_and_ride():
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                BIKE_MODES: httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_WET))

    response = TestClient(app).get("/v1/plan", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    kinds = {o["kind"] for o in body["options"]}
    assert kinds == {"bike", "transit", "bike_and_ride"}
    assert body["options"][0]["recommended"] is True
    assert body["recommendation"] == body["options"][0]["kind"]
    assert body["recommendation"] == "transit"
    # ranked by ascending score
    scores = [o["score"] for o in body["options"]]
    assert scores == sorted(scores)
