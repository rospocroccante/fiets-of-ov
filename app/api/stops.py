"""`GET /v1/stops` — GVB stops near a point, nearest first.

Thin controller: it validates the query, delegates the spatial lookup to
`app/services/stops.py`, and shapes the result. `radius` is in metres (default 500,
capped at 5000 — a stop-finder, not a city-wide scan). The param is `lon` (not the GTFS
`lng`) to stay consistent with `/v1/advice`.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.stop import StopOut
from app.services.stops import nearby_stops

router = APIRouter()


@router.get("/v1/stops", response_model=list[StopOut])
async def get_stops(
    lat: float = Query(ge=-90, le=90, description="Latitude of the search point"),
    lon: float = Query(ge=-180, le=180, description="Longitude of the search point"),
    radius: int = Query(500, ge=1, le=5000, description="Search radius in metres"),
    session: AsyncSession = Depends(get_session),
) -> list[StopOut]:
    """Return GVB stops within `radius` metres of `(lat, lon)`, nearest first."""
    results = await nearby_stops(session, lat=lat, lon=lon, radius_m=radius)
    return [
        StopOut(
            stop_id=stop.stop_id,
            code=stop.code,
            name=stop.name,
            lat=stop.lat,
            lon=stop.lon,
            location_type=stop.location_type,
            distance_m=round(distance, 1),
        )
        for stop, distance in results
    ]
