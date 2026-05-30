"""Pure great-circle helpers for nearby-stop search. No I/O — unit-tested offline.

The nearby-stop query is a two-step filter to stay index-friendly on stock Postgres
(no PostGIS): `bounding_box` gives degree bounds for a cheap `lat/lon BETWEEN` prefilter
that a btree index can serve, then `haversine_m` ranks the handful of candidates by exact
distance. A spherical earth is plenty accurate at city scale.
"""

import math

# Mean earth radius (metres). A sphere is accurate to well under 1% — far tighter than a
# stop-finder needs, and it keeps the bbox prefilter and the ranking on the same model.
EARTH_RADIUS_M = 6_371_000
_M_PER_DEG_LAT = math.pi / 180 * EARTH_RADIUS_M


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    # Clamp before asin: float rounding can push `a` a hair past 1.0 for near-antipodal
    # points, which would raise a math domain error.
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def bounding_box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Return `(lat_min, lat_max, lon_min, lon_max)` enclosing the radius around a point.

    The box is a safe **superset** of the circle (each axis uses the full radius), so it
    never excludes a stop that is actually within range — exact distance filtering then
    trims the corners. Longitude degrees shrink with latitude (×cos φ), so the lon span
    widens to compensate.
    """
    lat_delta = radius_m / _M_PER_DEG_LAT
    # Longitude degrees shrink toward the poles (×cos φ), so to stay a true superset of the
    # circle we size the lon span using the box's FARTHEST-from-equator latitude edge (the
    # widest physical degree), not the centre — otherwise a point within range but offset
    # poleward could fall just outside the box and be dropped before distance filtering.
    # The cosine is floored so the span never blows up to infinity right at a pole.
    far_lat = abs(lat) + lat_delta
    lon_delta = radius_m / (_M_PER_DEG_LAT * max(math.cos(math.radians(far_lat)), 1e-6))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta
