"""OTP client: turn the OpenTripPlanner `/plan` response into typed itineraries.

OTP is an untrusted upstream: every call is timed out, and anything other than a
clean itinerary response (HTTP error, transport failure, an `error` payload, or a
missing `plan`) must raise `OTPError` rather than fabricate a route. All HTTP is
mocked with respx so these run offline.
"""

import httpx
import pytest
import respx

from app.clients.otp import OTPClient, OTPError

URL = "https://otp.test/otp/routers/default"
PLAN_URL = f"{URL}/plan"

FROM = (52.3702, 4.8952)  # Amsterdam Centraal-ish
TO = (52.3580, 4.8686)  # Vondelpark-ish

BIKE_RESPONSE = {
    "plan": {
        "itineraries": [
            {
                "duration": 900,
                "startTime": 1_700_000_000_000,
                "endTime": 1_700_000_900_000,
                "legs": [
                    {
                        "mode": "BICYCLE",
                        "startTime": 1_700_000_000_000,
                        "endTime": 1_700_000_900_000,
                        "duration": 900,
                        "distance": 3200.0,
                        "routeShortName": None,
                    }
                ],
            }
        ]
    }
}

TRANSIT_RESPONSE = {
    "plan": {
        "itineraries": [
            {
                "duration": 720,
                "startTime": 1_700_000_000_000,
                "endTime": 1_700_000_720_000,
                "legs": [
                    {
                        "mode": "WALK",
                        "startTime": 1_700_000_000_000,
                        "endTime": 1_700_000_120_000,
                        "duration": 120,
                        "distance": 150.0,
                    },
                    {
                        "mode": "TRAM",
                        "startTime": 1_700_000_120_000,
                        "endTime": 1_700_000_720_000,
                        "duration": 600,
                        "distance": 2400.0,
                        "routeShortName": "13",
                    },
                ],
            }
        ]
    }
}


@respx.mock
async def test_plan_returns_typed_itinerary():
    respx.get(PLAN_URL).mock(return_value=httpx.Response(200, json=BIKE_RESPONSE))

    plan = await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")

    assert len(plan.itineraries) == 1
    itin = plan.itineraries[0]
    assert itin.duration == 900
    assert len(itin.legs) == 1
    assert itin.legs[0].mode == "BICYCLE"
    assert itin.legs[0].distance == 3200.0


@respx.mock
async def test_plan_parses_transit_route_short_name():
    respx.get(PLAN_URL).mock(return_value=httpx.Response(200, json=TRANSIT_RESPONSE))

    plan = await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="TRANSIT,WALK")

    legs = plan.itineraries[0].legs
    assert [leg.mode for leg in legs] == ["WALK", "TRAM"]
    assert legs[1].route_short_name == "13"
    assert legs[0].route_short_name is None


@respx.mock
async def test_plan_sends_coordinates_and_mode_as_params():
    route = respx.get(PLAN_URL).mock(return_value=httpx.Response(200, json=BIKE_RESPONSE))

    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")

    request = route.calls.last.request
    assert request.url.params["fromPlace"] == "52.3702,4.8952"
    assert request.url.params["toPlace"] == "52.358,4.8686"
    assert request.url.params["mode"] == "BICYCLE"


@respx.mock
async def test_raises_on_http_error_status():
    respx.get(PLAN_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_raises_on_otp_error_payload():
    respx.get(PLAN_URL).mock(
        return_value=httpx.Response(200, json={"error": {"msg": "No path found"}})
    )

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_raises_on_transport_error():
    respx.get(PLAN_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")
