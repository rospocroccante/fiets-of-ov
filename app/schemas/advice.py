"""Response schema for the advice endpoint.

This is the public contract of `GET /v1/advice`. The decision engine produces it
directly (it is a plain data object with no behaviour), and the router returns it
as-is, so the shape clients see is exactly what the engine decided.
"""

from typing import Literal

from pydantic import BaseModel


class AdviceResponse(BaseModel):
    """A bike-vs-OV recommendation with the numbers behind it and a human reason."""

    recommendation: Literal["bike", "transit", "bike_and_ride"]
    reason: str
    # Duration of the pure-bike route; None when OTP found no bike route.
    bike_minutes: int | None
    transit_minutes: int | None
    # Peak precipitation (mm/h) forecast during the cycling window; 0.0 when dry,
    # None when the rain forecast was unavailable (degraded answer).
    max_rain_mm_per_h: float | None
    # True/False when we have a forecast; None when rain data was unavailable.
    rain_expected: bool | None
    # True when we had no forecast at all and the recommendation is therefore weather-blind:
    # the ranking behind it was computed as if the whole trip were dry. The nullable fields
    # above already go None in that case, but "absent" is easy to read as "nothing to
    # report", so this states it outright — a client should show the answer with a caveat
    # ("couldn't check the weather") rather than as a confident dry-day recommendation.
    # False includes the stale-cache fallback: that is real, slightly old data, not a blank.
    forecast_degraded: bool = False
