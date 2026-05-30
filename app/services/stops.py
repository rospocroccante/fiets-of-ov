"""Nearby-stop lookup: the bounding-box prefilter + exact-distance ranking.

Stock Postgres has no PostGIS, so we keep it index-friendly: `bounding_box` gives degree
bounds for a `lat/lon BETWEEN …` query the composite (lat, lon) btree can serve, which
narrows ~1900 GVB stops to a handful; we then compute the exact great-circle distance for
those candidates, drop the ones outside the radius (the box's corners), and rank by
distance. At city scale the per-candidate Python distance is trivial.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stop import Stop
from app.services.geo import bounding_box, haversine_m


async def nearby_stops(
    session: AsyncSession,
    *,
    lat: float,
    lon: float,
    radius_m: float,
    limit: int = 50,
) -> list[tuple[Stop, float]]:
    """Return `(stop, distance_m)` for stops within `radius_m`, nearest first.

    The bounding box is a superset of the circle, so candidates outside the true radius
    are filtered out by exact distance before ranking.
    """
    lat_min, lat_max, lon_min, lon_max = bounding_box(lat, lon, radius_m)
    stmt = select(Stop).where(
        Stop.lat.between(lat_min, lat_max),
        Stop.lon.between(lon_min, lon_max),
    )
    candidates = (await session.execute(stmt)).scalars().all()

    within = [
        (stop, distance)
        for stop in candidates
        if (distance := haversine_m(lat, lon, stop.lat, stop.lon)) <= radius_m
    ]
    within.sort(key=lambda pair: pair[1])
    return within[:limit]
