"""Request/response schemas for trip alerts.

A trip alert is a recurring trip a user wants rain-aware notifications for.
`TripAlertCreate` is the client-supplied body (origin/destination are free-form place
strings; the service geocodes them into coordinates). `TripAlertOut` is the stored row
echoed back, including the resolved coordinates the resolver filled in.
"""

from datetime import datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TripAlertCreate(BaseModel):
    """Body for creating a trip alert.

    `origin`/`destination` are place strings (a name or `"lat,lon"`); the service resolves
    them to coordinates, so they are not supplied here. `days` are ISO weekdays (Mon=1..Sun=7).
    """

    label: str = Field(min_length=1, max_length=100)
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    departure_time: time
    days: list[int]

    @field_validator("label", "origin", "destination")
    @classmethod
    def _strip_nonblank(cls, value: str) -> str:
        """Trim surrounding whitespace and reject blank values (e.g. a spaces-only origin)."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("departure_time")
    @classmethod
    def _naive_local_time(cls, value: time) -> time:
        """Reject a timezone-aware time: the column is a naive local TIME, so an offset
        would be silently dropped on persist — surface it as a 422 instead."""
        if value.tzinfo is not None:
            raise ValueError("departure_time must be a naive local time (no timezone)")
        return value

    @field_validator("days")
    @classmethod
    def _validate_days(cls, days: list[int]) -> list[int]:
        """Normalise the recurrence days: non-empty, within ISO 1..7, deduplicated, sorted.

        An empty list would create a trip alert that never fires; out-of-range values are
        nonsensical weekdays; duplicates/oversized lists just bloat the row. We surface the
        caller's mistakes as 422 and store a clean canonical set.
        """
        if not days:
            raise ValueError("days must not be empty")
        if len(days) > 7:
            raise ValueError("days has at most 7 entries (one per weekday)")
        if any(day < 1 or day > 7 for day in days):
            raise ValueError("each day must be an ISO weekday in 1..7")
        return sorted(set(days))


class TripAlertOut(BaseModel):
    """Stored trip alert returned to clients, with resolver-filled coordinates.

    `from_attributes` lets FastAPI serialize a SQLAlchemy `TripAlert` row directly.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    origin: str
    origin_lat: float
    origin_lon: float
    destination: str
    dest_lat: float
    dest_lon: float
    departure_time: time
    days: list[int]
    created_at: datetime
