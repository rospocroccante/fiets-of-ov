"""Pure geo helpers for nearby-stop lookup: great-circle distance and a search bbox.

These are deliberately I/O-free so they're tested offline here; the DB layer only uses
them to pre-filter candidates (bounding box, index-friendly) and then rank by exact
distance. Expected values use independent physical references (metres per degree), not
the implementation's own output, so the tests can actually catch a wrong formula.
"""

import math

from app.services.geo import bounding_box, haversine_m

# One degree of latitude is ~111.19 km for a spherical earth of radius 6_371_000 m
# (pi/180 * 6_371_000). Used as an implementation-independent reference below.
M_PER_DEG_LAT = math.pi / 180 * 6_371_000


def test_haversine_zero_distance():
    assert haversine_m(52.37, 4.90, 52.37, 4.90) == 0.0


def test_haversine_one_degree_latitude():
    # Along a meridian, 1° latitude ≈ 111.19 km regardless of longitude.
    d = haversine_m(52.0, 4.9, 53.0, 4.9)
    assert math.isclose(d, M_PER_DEG_LAT, rel_tol=1e-3)


def test_haversine_longitude_shrinks_with_latitude():
    # 1° of longitude at latitude φ is ~cos(φ) of a degree at the equator.
    at_equator = haversine_m(0.0, 0.0, 0.0, 1.0)
    at_lat60 = haversine_m(60.0, 0.0, 60.0, 1.0)
    assert math.isclose(at_lat60, at_equator * math.cos(math.radians(60)), rel_tol=1e-3)


def test_haversine_is_symmetric():
    a = haversine_m(52.3791, 4.9003, 52.3579, 4.8686)
    b = haversine_m(52.3579, 4.8686, 52.3791, 4.9003)
    assert a == b
    assert 2000 < a < 4000  # Centraal↔Vondelpark straight line, sanity band


def test_bounding_box_latitude_span_matches_radius():
    lat_min, lat_max, _, _ = bounding_box(52.0, 4.9, radius_m=1000)
    # Half-span north/south should be 1000 m expressed in degrees of latitude.
    assert math.isclose(lat_max - 52.0, 1000 / M_PER_DEG_LAT, rel_tol=1e-3)
    assert math.isclose(52.0 - lat_min, 1000 / M_PER_DEG_LAT, rel_tol=1e-3)


def test_bounding_box_longitude_span_widens_with_radius_and_latitude():
    _, _, lon_min, lon_max = bounding_box(52.0, 4.9, radius_m=1000)
    expected = 1000 / (M_PER_DEG_LAT * math.cos(math.radians(52.0)))
    assert math.isclose(lon_max - 4.9, expected, rel_tol=1e-3)
    assert math.isclose(4.9 - lon_min, expected, rel_tol=1e-3)


def _destination(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Point `dist_m` from (lat, lon) along `bearing_deg`, on the same sphere as haversine."""
    d = dist_m / (M_PER_DEG_LAT * 180 / math.pi)  # angular distance (rad); R cancels out
    br, p1, l1 = math.radians(bearing_deg), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), math.degrees(l2)


def test_bounding_box_is_a_superset_at_every_bearing():
    # The bbox must enclose the whole circle, including the east-west axis where longitude
    # degrees are widest at the box's poleward edge. Sweep all bearings at two latitudes —
    # Amsterdam, and a high latitude + large radius where a centre-cos bbox visibly excludes
    # points (tens of metres of overshoot, far above the tiny epsilon below).
    eps = 1e-9  # ~0.1 mm: with the far-edge lon span the box is a strict superset (ULP only)
    for lat, radius in ((52.37, 2000), (80.0, 5000)):
        lat_min, lat_max, lon_min, lon_max = bounding_box(lat, 4.90, radius_m=radius)
        for bearing in range(360):
            plat, plon = _destination(lat, 4.90, bearing, radius)
            assert lat_min - eps <= plat <= lat_max + eps, (lat, bearing)
            assert lon_min - eps <= plon <= lon_max + eps, (lat, bearing)


def test_haversine_does_not_raise_on_near_antipodal_points():
    # Float rounding can push the haversine radicand just past 1.0; it must clamp, not crash.
    d = haversine_m(0.0, 0.0, 0.5, 179.9999999)
    assert d > 0  # ~half the earth's circumference, no math domain error
