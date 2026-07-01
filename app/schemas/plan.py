"""Response schema for `GET /v1/plan` — the rich, drawable trip plan.

Where `/v1/advice` returns only the recommendation and the numbers behind it, `/v1/plan`
additionally returns both full itineraries (bike and, when available, public transport),
each as an ordered list of legs with an encoded polyline and per-leg detail. That is what
a map UI needs to draw the route and list step-by-step directions, so the client never has
to talk to the router directly.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlaceOut(BaseModel):
    """A point on the map: a name (for transit stops) and coordinates."""

    name: str | None = None
    lat: float | None = None
    lon: float | None = None


class StepOut(BaseModel):
    """One turn-by-turn instruction within a street (walk/bike) leg."""

    distance_m: float | None = None
    direction: str | None = None
    street: str | None = None


class LegOut(BaseModel):
    """One leg of an itinerary: a single mode between two points, with its geometry."""

    model_config = ConfigDict(populate_by_name=True)

    mode: str
    minutes: int
    distance_m: float | None = None
    # Transit only: line short name (e.g. "13"), long name, and headsign; None off-transit.
    route: str | None = None
    route_long_name: str | None = None
    headsign: str | None = None
    # `from` is a Python keyword, so the field is `origin` but serializes as JSON "from".
    origin: PlaceOut = Field(alias="from")
    to: PlaceOut
    # Encoded polyline (Google polyline, precision 5) for drawing this leg.
    geometry: str | None = None
    start_time: int
    end_time: int
    steps: list[StepOut] = Field(default_factory=list)


class ItineraryOut(BaseModel):
    """A full door-to-door option: total time/distance plus its ordered legs."""

    minutes: int
    distance_m: float
    start_time: int
    end_time: int
    legs: list[LegOut]


OptionKind = Literal["bike", "transit", "bike_and_ride"]


class OptionOut(BaseModel):
    """One ranked door-to-door option with its drawable itinerary."""

    kind: OptionKind
    recommended: bool
    score: float  # generalized cost (sort aid; clients may ignore)
    rain_minutes: int  # cycling/walking minutes exposed to rain (0 if dry)
    itinerary: ItineraryOut


class PlanResponse(BaseModel):
    """The recommendation plus the full ranked list of drawable options."""

    recommendation: OptionKind
    reason: str
    max_rain_mm_per_h: float | None
    rain_expected: bool | None
    origin: PlaceOut
    destination: PlaceOut
    options: list[OptionOut]  # ranked, recommended first
