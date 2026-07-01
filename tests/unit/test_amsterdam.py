"""Unit tests for app.services.amsterdam — pure local-knowledge module.

All tests are offline (no I/O). Leg/Itinerary are constructed directly following
the pattern in tests/unit/test_planner.py.
"""

import pytest

from app.clients.otp import Itinerary, Leg
from app.services.amsterdam import (
    bike_handoff_point,
    has_ferry,
    is_near_hub,
    transfer_points,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def leg(
    mode: str,
    *,
    from_lat: float | None = None,
    from_lon: float | None = None,
    to_lat: float | None = None,
    to_lon: float | None = None,
) -> Leg:
    return Leg(
        mode=mode,
        start_time=0,
        end_time=600_000,
        duration=600.0,
        distance=1000.0,
        from_lat=from_lat,
        from_lon=from_lon,
        to_lat=to_lat,
        to_lon=to_lon,
    )


def itin(*legs: Leg, duration: float = 600.0) -> Itinerary:
    return Itinerary(
        duration=duration,
        start_time=0,
        end_time=int(duration * 1000),
        legs=list(legs),
    )


# ---------------------------------------------------------------------------
# is_near_hub
# ---------------------------------------------------------------------------


def test_is_near_hub_true_centraal():
    # Amsterdam Centraal at (52.3791, 4.9003) — distance to itself is 0, within 250 m.
    assert is_near_hub(52.3791, 4.9003) is True


def test_is_near_hub_false_far_away():
    # A point well outside Amsterdam is not near any hub.
    assert is_near_hub(52.30, 4.75) is False


def test_is_near_hub_none_lat():
    assert is_near_hub(None, 4.9) is False


def test_is_near_hub_none_lon():
    assert is_near_hub(52.3791, None) is False


def test_is_near_hub_none_both():
    assert is_near_hub(None, None) is False


def test_is_near_hub_true_near_centraal():
    # A point ~100 m south of Centraal should still be within 250 m.
    # Approx 0.001 degree lat ~ 111 m.
    assert is_near_hub(52.3781, 4.9003) is True


# ---------------------------------------------------------------------------
# has_ferry
# ---------------------------------------------------------------------------


def test_has_ferry_true():
    it = itin(
        leg("WALK"),
        leg("FERRY", from_lat=52.38, from_lon=4.90, to_lat=52.40, to_lon=4.92),
        leg("WALK"),
    )
    assert has_ferry(it) is True


def test_has_ferry_false():
    it = itin(leg("WALK"), leg("TRAM"), leg("WALK"))
    assert has_ferry(it) is False


def test_has_ferry_empty():
    assert has_ferry(itin()) is False


# ---------------------------------------------------------------------------
# transfer_points
# ---------------------------------------------------------------------------


def test_transfer_points_single_transit_leg_returns_empty():
    # One transit leg: no transfer.
    it = itin(leg("WALK"), leg("TRAM", from_lat=52.36, from_lon=4.88))
    assert transfer_points(it) == []


def test_transfer_points_two_transit_legs_returns_one():
    # Two consecutive transit legs: one transfer at the from-point of the second.
    it = itin(
        leg("WALK"),
        leg("TRAM", from_lat=52.36, from_lon=4.88),
        leg("SUBWAY", from_lat=52.3791, from_lon=4.9003),
    )
    pts = transfer_points(it)
    assert len(pts) == 1
    assert pts[0] == pytest.approx((52.3791, 4.9003))


def test_transfer_points_three_transit_legs_returns_two():
    it = itin(
        leg("TRAM", from_lat=52.35, from_lon=4.87),
        leg("BUS", from_lat=52.36, from_lon=4.88),
        leg("SUBWAY", from_lat=52.3791, from_lon=4.9003),
    )
    pts = transfer_points(it)
    assert len(pts) == 2
    assert pts[0] == pytest.approx((52.36, 4.88))
    assert pts[1] == pytest.approx((52.3791, 4.9003))


def test_transfer_points_walk_between_transit_does_not_add_point():
    # WALK between transit legs is not a transit leg so does not add a transfer point;
    # only the transit legs after the first transit leg count.
    it = itin(
        leg("TRAM", from_lat=52.35, from_lon=4.87),
        leg("WALK"),
        leg("SUBWAY", from_lat=52.3791, from_lon=4.9003),
    )
    pts = transfer_points(it)
    # Still one transfer: the SUBWAY leg is the second transit leg.
    assert len(pts) == 1
    assert pts[0] == pytest.approx((52.3791, 4.9003))


def test_transfer_points_none_coords_skipped():
    # If the transfer leg's from coords are None, that point must be omitted.
    it = itin(
        leg("TRAM", from_lat=52.35, from_lon=4.87),
        leg("SUBWAY", from_lat=None, from_lon=None),
    )
    pts = transfer_points(it)
    assert pts == []


# ---------------------------------------------------------------------------
# bike_handoff_point
# ---------------------------------------------------------------------------


def test_bike_handoff_point_returns_to_coords():
    it = itin(
        leg("BICYCLE", to_lat=52.3791, to_lon=4.9003),
        leg("SUBWAY", from_lat=52.3791, from_lon=4.9003),
    )
    pt = bike_handoff_point(it)
    assert pt == pytest.approx((52.3791, 4.9003))


def test_bike_handoff_point_none_when_no_bike_leg():
    it = itin(leg("WALK"), leg("TRAM"), leg("WALK"))
    assert bike_handoff_point(it) is None


def test_bike_handoff_point_none_when_to_coords_missing():
    it = itin(leg("BICYCLE", to_lat=None, to_lon=None))
    assert bike_handoff_point(it) is None


def test_bike_handoff_point_first_bike_leg_only():
    # Multiple bike legs: only the first one's to-coords are returned.
    it = itin(
        leg("BICYCLE", to_lat=52.36, to_lon=4.88),
        leg("WALK"),
        leg("BICYCLE", to_lat=52.40, to_lon=4.92),
    )
    pt = bike_handoff_point(it)
    assert pt == pytest.approx((52.36, 4.88))
