"""OTP client: turn the OTP2 GTFS GraphQL `plan` response into typed itineraries.

OTP is an untrusted upstream: every call is timed out, and anything other than a clean
itinerary response (HTTP error, transport failure, GraphQL `errors`, or a null `plan`)
must raise `OTPError` rather than fabricate a route. The GraphQL POST is mocked with
respx so these run offline.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.clients.otp import OTPClient, OTPError

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
async def test_plan_sends_departure_date_and_time():
    # A scheduled trip-alert evaluation plans the bike leg *at the departure time*, not
    # "now". OTP's plan() takes date/time strings (local to the OTP graph's timezone), so
    # the client must split the departure datetime into "YYYY-MM-DD" and "HH:MM:SS".
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    departure = datetime(2026, 5, 31, 8, 7, 5, tzinfo=ZoneInfo("Europe/Amsterdam"))
    await OTPClient(base_url=URL).plan(
        from_place=FROM, to_place=TO, mode="BICYCLE", departure=departure
    )

    variables = json.loads(route.calls.last.request.content)["variables"]
    assert variables["date"] == "2026-05-31"
    assert variables["time"] == "08:07:05"


@respx.mock
async def test_plan_sends_null_date_and_time_when_no_departure():
    # Without a departure, OTP must plan from "now": send null for both so the existing
    # callers (and OTP's default) are unchanged.
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")

    variables = json.loads(route.calls.last.request.content)["variables"]
    assert variables["date"] is None
    assert variables["time"] is None


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
async def test_raises_on_null_plan():
    # A 200 with {"data": {"plan": null}} (OTP answered but produced no plan object)
    # must raise OTPError, not slip through as an empty result.
    respx.post(GQL_URL).mock(return_value=httpx.Response(200, json={"data": {"plan": None}}))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_raises_on_transport_error():
    respx.post(GQL_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_raises_on_malformed_itinerary():
    # A 200 response whose body doesn't match the expected shape (here an itinerary missing
    # "duration") must raise OTPError, not leak a KeyError/ValidationError to the caller —
    # OTP is untrusted, so a malformed-but-200 response is just another failure mode.
    bad = gql([{"startTime": 1, "endTime": 2, "legs": []}])  # no "duration"
    respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=bad))

    with pytest.raises(OTPError):
        await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")


@respx.mock
async def test_plan_sends_tuned_routing_params():
    """Amsterdam-tuned parameters are sent to OTP on every plan call."""
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")

    variables = json.loads(route.calls.last.request.content)["variables"]
    assert variables["num"] == 12
    assert variables["searchWindow"] == 3600
    assert variables["walkReluctance"] == 2.5
    assert variables["bikeSpeed"] == 5.0
    assert variables["transferPenalty"] == 180


@respx.mock
async def test_bike_triangle_only_for_bike_modes():
    """optimize/triangle are sent for BICYCLE-containing modes and absent for transit-only."""
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    # BICYCLE mode: triangle must be present
    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")
    variables = json.loads(route.calls.last.request.content)["variables"]
    assert variables["optimize"] == "TRIANGLE"
    assert variables["triangle"] == {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}

    # BICYCLE,TRANSIT,WALK mode: triangle must be present
    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE,TRANSIT,WALK")
    variables = json.loads(route.calls.last.request.content)["variables"]
    assert variables["optimize"] == "TRIANGLE"
    assert variables["triangle"] == {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}

    # TRANSIT,WALK mode: optimize and triangle must be None
    respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=TRANSIT))
    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="TRANSIT,WALK")
    variables = json.loads(route.calls.last.request.content)["variables"]
    assert variables["optimize"] is None
    assert variables["triangle"] is None


@respx.mock
async def test_slim_plan_overrides_num_and_drops_geometry_and_steps():
    # The snap fallback probes with a slim variant: fewer itineraries and no
    # legGeometry/steps selection, so 24 probes don't each pay for full geometry.
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    plan = await OTPClient(base_url=URL).plan(
        from_place=FROM, to_place=TO, mode="BICYCLE", num_itineraries=3, slim=True
    )

    body = json.loads(route.calls.last.request.content)
    assert body["variables"]["num"] == 3
    assert "legGeometry" not in body["query"]
    assert "steps" not in body["query"]
    assert plan.itineraries[0].legs[0].mode == "BICYCLE"


@respx.mock
async def test_reuses_one_http_client_across_calls():
    # The AsyncClient is built lazily once and shared (connection pooling); aclose()
    # releases it so a fresh one would be created on next use.
    respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))
    client = OTPClient(base_url=URL)

    await client.plan(from_place=FROM, to_place=TO, mode="BICYCLE")
    shared = client._client
    assert shared is not None
    await client.plan(from_place=FROM, to_place=TO, mode="BICYCLE")
    assert client._client is shared

    await client.aclose()
    assert client._client is None


@respx.mock
async def test_full_plan_still_selects_geometry_and_steps():
    # The default (non-slim) query must keep the drawable detail /v1/plan relies on.
    route = respx.post(GQL_URL).mock(return_value=httpx.Response(200, json=BIKE))

    await OTPClient(base_url=URL).plan(from_place=FROM, to_place=TO, mode="BICYCLE")

    body = json.loads(route.calls.last.request.content)
    assert body["variables"]["num"] == 12
    assert "legGeometry" in body["query"]
    assert "steps" in body["query"]
