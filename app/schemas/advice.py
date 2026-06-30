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
    bike_minutes: int
    transit_minutes: int | None
    # Peak precipitation (mm/h) forecast during the cycling window; 0.0 when dry,
    # None when the rain forecast was unavailable (degraded answer).
    max_rain_mm_per_h: float | None
    # True/False when we have a forecast; None when rain data was unavailable.
    rain_expected: bool | None
