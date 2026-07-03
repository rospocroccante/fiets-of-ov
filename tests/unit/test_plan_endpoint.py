"""`GET /v1/plan` — the rich, drawable counterpart to `/v1/advice`.

Same wiring as the advice endpoint (shared harness in tests/unit/conftest.py: OTP +
Buienradar mocked with respx, clients swapped via dependency overrides), but the leg
fixtures carry the extra detail `/v1/plan` exposes -- `from`/`to` places, `legGeometry`,
route long name, headsign -- so the test asserts the full drawable shape, not just the
recommendation.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.clients.otp import BIKE_MODES
from app.main import app
from tests.unit.conftest import GQL_URL, RAIN_DRY, RAIN_URL, RAIN_WET, _gql, _otp_by_modes, ms

pytestmark = pytest.mark.usefixtures("_override_clients")


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
