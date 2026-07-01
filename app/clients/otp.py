"""Async client for OpenTripPlanner's OTP2 **GTFS GraphQL** API (`/otp/gtfs/v1`).

OTP does the multimodal routing; this wrapper adapts it to typed data and enforces our
trust boundary. OTP2 dropped the old REST `/plan` endpoint in favour of GraphQL, so we
POST a `plan` query and read `data.plan.itineraries`. Things to know:

- OTP is treated as **untrusted**. Every call is bounded by an explicit timeout, and any
  failure mode — non-2xx status, a transport error, GraphQL `errors`, or a null `plan` —
  raises `OTPError`. We never invent a route on failure; callers surface a clear error.
- Itinerary/leg `startTime`/`endTime` are **epoch milliseconds**; we keep them as ints
  rather than guessing a timezone here. The decision engine reconciles them with the
  Buienradar forecast.
- A leg's `route` is null for non-transit legs (walk/bike); for transit legs we read
  `route.shortName` (e.g. "13" for tram 13).
- `mode` is a comma-separated string ("BICYCLE", "TRANSIT,WALK") mapped to the GraphQL
  `transportModes` list, so callers don't need to know the GraphQL shape.
"""

from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings

# GTFS GraphQL endpoint path appended to the OTP host root.
_GRAPHQL_PATH = "/otp/gtfs/v1"

# Amsterdam-tuned OTP routing parameters (see design doc). All verified present on the
# running OTP2 GTFS schema.
_NUM_ITINERARIES = 5
_SEARCH_WINDOW_S = 5400
_WALK_RELUCTANCE = 2.5
_WALK_SPEED = 1.35
_BIKE_SPEED = 4.3
_BIKE_RELUCTANCE = 1.7
_TRANSFER_PENALTY = 180
_WALK_BOARD_COST = 600
_BIKE_TRIANGLE = {"safety": 0.4, "slope": 0.3, "time": 0.3}

# `startTime`/`endTime` are epoch-ms Longs in the GTFS API; `route` is null off-transit.
# `$date`/`$time` are optional plan-from-when strings (local to the OTP graph's tz); when
# both are null OTP plans from "now", so existing "plan now" callers are unaffected.
# `$optimize`/`$triangle` are only sent for BICYCLE-containing modes (see plan()).
_PLAN_QUERY = """
query Plan(
  $from: InputCoordinates!
  $to: InputCoordinates!
  $modes: [TransportMode!]
  $num: Int
  $date: String
  $time: String
  $searchWindow: Long
  $walkReluctance: Float
  $walkSpeed: Float
  $bikeSpeed: Float
  $bikeReluctance: Float
  $transferPenalty: Int
  $walkBoardCost: Int
  $optimize: OptimizeType
  $triangle: InputTriangle
) {
  plan(
    from: $from
    to: $to
    transportModes: $modes
    numItineraries: $num
    date: $date
    time: $time
    searchWindow: $searchWindow
    walkReluctance: $walkReluctance
    walkSpeed: $walkSpeed
    bikeSpeed: $bikeSpeed
    bikeReluctance: $bikeReluctance
    transferPenalty: $transferPenalty
    walkBoardCost: $walkBoardCost
    optimize: $optimize
    triangle: $triangle
  ) {
    itineraries {
      duration
      startTime
      endTime
      legs {
        mode
        startTime
        endTime
        duration
        distance
        route { shortName longName }
        trip { tripHeadsign }
        from { name lat lon }
        to { name lat lon }
        legGeometry { points }
        steps { distance relativeDirection streetName }
      }
    }
  }
}
"""


class OTPError(Exception):
    """Raised when OTP cannot be reached or did not return a usable itinerary."""


class LegStep(BaseModel):
    """One turn-by-turn instruction within a street (walk/bike) leg."""

    model_config = ConfigDict(populate_by_name=True)

    distance: float | None = None
    relative_direction: str | None = Field(default=None, alias="relativeDirection")
    street_name: str | None = Field(default=None, alias="streetName")


class Leg(BaseModel):
    """One leg of an itinerary (a single mode between two points)."""

    model_config = ConfigDict(populate_by_name=True)

    mode: str
    start_time: int = Field(alias="startTime")
    end_time: int = Field(alias="endTime")
    duration: float
    distance: float | None = None
    # Present only for transit legs (e.g. "13" for tram 13); None for bike/walk.
    route_short_name: str | None = Field(default=None, alias="routeShortName")
    # Optional display detail (used by /v1/plan; the advice engine ignores these).
    route_long_name: str | None = None
    headsign: str | None = None
    from_name: str | None = None
    from_lat: float | None = None
    from_lon: float | None = None
    to_name: str | None = None
    to_lat: float | None = None
    to_lon: float | None = None
    # Encoded polyline (Google polyline, precision 5) for drawing the leg on a map.
    geometry: str | None = None
    # Turn-by-turn steps for street legs (walk/bike); empty for transit.
    steps: list[LegStep] = Field(default_factory=list)


class Itinerary(BaseModel):
    """A full door-to-door option: total duration plus its ordered legs."""

    model_config = ConfigDict(populate_by_name=True)

    duration: float
    start_time: int = Field(alias="startTime")
    end_time: int = Field(alias="endTime")
    legs: list[Leg]


class Plan(BaseModel):
    """The candidate itineraries returned for a trip."""

    itineraries: list[Itinerary]


def _to_transport_modes(mode: str) -> list[dict[str, str]]:
    """Map a comma-separated mode string to the GraphQL `transportModes` list."""
    return [{"mode": part.strip()} for part in mode.split(",") if part.strip()]


def _leg_from_graphql(raw: dict) -> Leg:
    """Build a Leg from a GraphQL leg node, flattening nested route/trip/place/geometry."""
    route = raw.get("route") or {}
    trip = raw.get("trip") or {}
    origin = raw.get("from") or {}
    dest = raw.get("to") or {}
    geometry = raw.get("legGeometry") or {}
    steps = [
        LegStep(
            distance=s.get("distance"),
            relative_direction=s.get("relativeDirection"),
            street_name=s.get("streetName"),
        )
        for s in (raw.get("steps") or [])
    ]
    return Leg(
        mode=raw["mode"],
        start_time=raw["startTime"],
        end_time=raw["endTime"],
        duration=raw["duration"],
        distance=raw.get("distance"),
        route_short_name=route.get("shortName"),
        route_long_name=route.get("longName"),
        headsign=trip.get("tripHeadsign"),
        from_name=origin.get("name"),
        from_lat=origin.get("lat"),
        from_lon=origin.get("lon"),
        to_name=dest.get("name"),
        to_lat=dest.get("lat"),
        to_lon=dest.get("lon"),
        geometry=geometry.get("points"),
        steps=steps,
    )


class OTPClient:
    """Queries a self-hosted OTP2 instance for itineraries via the GTFS GraphQL API."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        root = (base_url or settings.otp_base_url).rstrip("/")
        self._graphql_url = f"{root}{_GRAPHQL_PATH}"
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds

    async def plan(
        self,
        *,
        from_place: tuple[float, float],
        to_place: tuple[float, float],
        mode: str,
        departure: datetime | None = None,
    ) -> Plan:
        """Return OTP's itineraries for `from_place` -> `to_place` using `mode`.

        `from_place`/`to_place` are `(lat, lon)`; `mode` is a comma-separated mode string
        (e.g. "BICYCLE", "TRANSIT,WALK").

        `departure` pins the plan to a specific moment (e.g. a scheduled rain-alert trip):
        it is split into OTP's separate `date`/`time` strings. When omitted, both are sent
        as null so OTP plans from "now" — keeping existing "plan now" callers unchanged.

        Raises `OTPError` on any failure so the caller never receives a fabricated route.
        """
        is_bike_mode = "BICYCLE" in mode
        variables = {
            "from": {"lat": from_place[0], "lon": from_place[1]},
            "to": {"lat": to_place[0], "lon": to_place[1]},
            "modes": _to_transport_modes(mode),
            "num": _NUM_ITINERARIES,
            # OTP expects date/time as separate strings; null on both => plan from "now".
            "date": departure.strftime("%Y-%m-%d") if departure is not None else None,
            "time": departure.strftime("%H:%M:%S") if departure is not None else None,
            # Amsterdam-tuned routing parameters.
            "searchWindow": _SEARCH_WINDOW_S,
            "walkReluctance": _WALK_RELUCTANCE,
            "walkSpeed": _WALK_SPEED,
            "bikeSpeed": _BIKE_SPEED,
            "bikeReluctance": _BIKE_RELUCTANCE,
            "transferPenalty": _TRANSFER_PENALTY,
            "walkBoardCost": _WALK_BOARD_COST,
            # Safe bike routing only applies to modes that include BICYCLE.
            "optimize": "TRIANGLE" if is_bike_mode else None,
            "triangle": _BIKE_TRIANGLE if is_bike_mode else None,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._graphql_url, json={"query": _PLAN_QUERY, "variables": variables}
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            # Covers connect/read timeouts, connection errors, and non-2xx statuses.
            raise OTPError(f"OTP request failed: {exc}") from exc

        if data.get("errors"):
            raise OTPError(f"OTP GraphQL errors: {data['errors']}")
        plan = (data.get("data") or {}).get("plan")
        if plan is None:
            raise OTPError("OTP returned no plan")

        # OTP is untrusted: a 200 whose body doesn't match the expected shape is just another
        # failure mode. Wrap parsing so a missing field / bad type surfaces as OTPError rather
        # than leaking a KeyError/ValidationError to callers (e.g. the notify sweep, which only
        # guards against OTPError).
        try:
            itineraries = [
                Itinerary(
                    duration=it["duration"],
                    start_time=it["startTime"],
                    end_time=it["endTime"],
                    legs=[_leg_from_graphql(leg) for leg in it["legs"]],
                )
                for it in plan.get("itineraries", [])
            ]
        except (KeyError, TypeError, ValidationError) as exc:
            raise OTPError(f"OTP returned a malformed itinerary: {exc}") from exc
        return Plan(itineraries=itineraries)
