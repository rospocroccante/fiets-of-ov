"""Response schema for the nearby-stops endpoint."""

from pydantic import BaseModel


class StopOut(BaseModel):
    """A GVB stop plus its distance from the query point."""

    stop_id: str
    code: str | None
    name: str
    lat: float
    lon: float
    location_type: int
    # Great-circle distance from the requested (lat, lon), in metres.
    distance_m: float
