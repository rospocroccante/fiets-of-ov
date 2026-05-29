"""OTP client: turn the OTP2 GTFS GraphQL `plan` response into typed itineraries.

OTP is an untrusted upstream: every call is timed out, and anything other than a clean
itinerary response (HTTP error, transport failure, GraphQL `errors`, or a null `plan`)
must raise `OTPError` rather than fabricate a route. The GraphQL POST is mocked with
respx so these run offline.
"""

import json

import httpx
import pytest
import respx

from app.clients.otp import Itinerary, Leg, OTPClient, OTPError, Plan, first_transit_itinerary

URL = "http://otp.test"  # host root; the client appends /otp/gtfs/v1
GQL_URL = f"{URL}/otp/gtfs/v1"

FROM = (52.3702, 4.8952)
TO = (52.3580, 4.8686)


def gql(itineraries: list[dict]) -> dict:
    return {"data": {"plan": {"itineraries": itineraries}}}


BIKE = gql(
    [
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
                    "route": None,
                }
            ],
        }
    ]
)

TRANSIT = gql(
    [
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
                    "route": None,
                },
                {
                    "mode": "TRAM",
                    "startTime": 1_700_000_120_000,
                    "endTime": 1_700_000_720_000,
                    "duration": 600,
                    "distance": 2400.0,
                    "route": {"shortName": "13"},
                },
            ],
        }
    ]
)


@respx.mock
async def test_plan_returns_typed_itinerary():
    respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    plan = await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")

    assert len(plan.itineraries) == 1
    itin = plan.itineraries[0]
    assert itin.duration == 900
    assert len(itin.legs) == 1
    assert itin.legs[0].mode == "BICYCLE"
    assert itin.legs[0].distance == 3200.0
    assert itin.legs[0].route_short_name is None


@respx.mock
async def test_plan_parses_transit_route_short_name():
    respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=TRANSIT))

    plan = await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="TRANSIT,WALK")

    legs = plan.itineraries[0].legs
    assert [leg.mode for leg in legs] == ["WALK", "TRAM"]
    assert legs[1].route_short_name == "13"
    assert legs[0].route_short_name is None


@respx.mock
async def test_plan_sends_graphql_coordinates_and_modes():
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="TRANSIT,WALK")

    body = json.loads(route.calls.last.request.content)
    variables = body["variables"]
    assert variables["from"] == {"lat": 52.3702, "lon": 4.8952}
    assert variables["to"] == {"lat": 52.358, "lon": 4.8686}
    assert variables["modes"] == [{"mode": "TRANSIT"}, {"mode": "WALK"}]


@respx.mock
async def test_raises_on_http_error_status():
    respx.post(GQL_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_raises_on_graphql_errors():
    respx.post(GQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})
    )

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_raises_on_transport_error():
    respx.post(GQL_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


def _itin(*modes: str) -> Itinerary:
    """Build an itinerary with one leg per mode; times are irrelevant to leg selection."""
    legs = [Leg(mode=m, start_time=0, end_time=0, duration=0.0) for m in modes]
    return Itinerary(duration=0.0, start_time=0, end_time=0, legs=legs)


def test_first_transit_itinerary_skips_walk_only_fallback():
    # OTP lists the WALK-only fallback first; the tram itinerary must be chosen.
    walk_only = _itin("WALK")
    tram = _itin("WALK", "TRAM")
    assert first_transit_itinerary(Plan(itineraries=[walk_only, tram])) is tram


def test_first_transit_itinerary_none_when_only_walking():
    assert first_transit_itinerary(Plan(itineraries=[_itin("WALK")])) is None


def test_first_transit_itinerary_none_when_empty():
    assert first_transit_itinerary(Plan(itineraries=[])) is None
