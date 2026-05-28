"""Async client for OpenTripPlanner's classic REST `/plan` endpoint.

OTP does the multimodal routing; this wrapper only adapts it to typed data and
enforces our trust boundary. Things to know:

- OTP is treated as **untrusted**. Every call is bounded by an explicit timeout, and
  any failure mode — non-2xx status, a transport error, an `error` payload, or a
  response with no `plan` — raises `OTPError`. We never invent a route on failure;
  callers surface a clear error instead.
- Times come back as **epoch milliseconds** (`startTime`/`endTime`). We keep them as
  ints rather than guessing a timezone here; the decision engine reconciles them with
  the Buienradar forecast in a later phase.
- The JSON uses camelCase; the models map it to snake_case via field aliases.
"""

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings


class OTPError(Exception):
    """Raised when OTP cannot be reached or did not return a usable itinerary."""


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


class Itinerary(BaseModel):
    """A full door-to-door option: total duration plus its ordered legs."""

    model_config = ConfigDict(populate_by_name=True)

    duration: float
    start_time: int = Field(alias="startTime")
    end_time: int = Field(alias="endTime")
    legs: list[Leg]


class Plan(BaseModel):
    """The `plan` block of an OTP response: the candidate itineraries."""

    itineraries: list[Itinerary]


class OTPClient:
    """Queries OTP for itineraries between two coordinates for a given mode."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self._base_url = base_url or settings.otp_base_url
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds

    async def plan(
        self,
        *,
        from_place: tuple[float, float],
        to_place: tuple[float, float],
        mode: str,
    ) -> Plan:
        """Return OTP's itineraries for `from_place` -> `to_place` using `mode`.

        `from_place`/`to_place` are `(lat, lon)`; `mode` is an OTP mode string
        (e.g. "BICYCLE", "TRANSIT,WALK"). Raises `OTPError` on any failure so the
        caller never receives a fabricated route.
        """
        params = {
            "fromPlace": f"{from_place[0]},{from_place[1]}",
            "toPlace": f"{to_place[0]},{to_place[1]}",
            "mode": mode,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base_url}/plan", params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            # Covers connect/read timeouts, connection errors, and non-2xx statuses.
            raise OTPError(f"OTP request failed: {exc}") from exc

        if data.get("error") or "plan" not in data:
            raise OTPError(f"OTP returned no itinerary: {data.get('error') or 'missing plan'}")

        return Plan.model_validate(data["plan"])
