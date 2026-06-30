import pytest

from app.clients.otp import Itinerary, Leg, OTPError, Plan
from app.services.planner import gather_candidates


def leg(mode: str, route: str | None = None) -> Leg:
    return Leg(
        mode=mode,
        start_time=0,
        end_time=600_000,
        duration=600.0,
        distance=1000.0,
        route_short_name=route,
    )


def itin(*legs: Leg, duration: float = 600.0) -> Itinerary:
    return Itinerary(
        duration=duration, start_time=0, end_time=int(duration * 1000), legs=list(legs)
    )


class FakeOTP:
    """Answers plan() by the mode string; modes not in the map raise OTPError."""

    def __init__(self, by_mode: dict[str, Plan], fail: set[str] | None = None):
        self.by_mode = by_mode
        self.fail = fail or set()
        self.calls: list[str] = []

    async def plan(self, *, from_place, to_place, mode, departure=None) -> Plan:
        self.calls.append(mode)
        if mode in self.fail:
            raise OTPError(f"boom {mode}")
        return self.by_mode.get(mode, Plan(itineraries=[]))


async def test_gathers_one_best_candidate_per_kind():
    otp = FakeOTP(
        {
            "BICYCLE": Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)]),
            "TRANSIT,WALK": Plan(
                itineraries=[itin(leg("WALK"), leg("TRAM", "13"), duration=720.0)]
            ),
            "BICYCLE,TRANSIT,WALK": Plan(
                itineraries=[itin(leg("BICYCLE"), leg("SUBWAY", "52"), duration=900.0)]
            ),
        }
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    kinds = sorted(c.kind for c in candidates)
    assert kinds == ["bike", "bike_and_ride", "transit"]
    assert set(otp.calls) == {"BICYCLE", "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"}


async def test_walk_only_itineraries_are_dropped():
    otp = FakeOTP({"TRANSIT,WALK": Plan(itineraries=[itin(leg("WALK"), duration=2400.0)])})
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert candidates == []  # walk-only is no option; nothing else routed


async def test_keeps_shortest_per_kind_across_queries():
    # Both the transit query and the mixed query yield a transit itinerary; keep the shorter.
    otp = FakeOTP(
        {
            "TRANSIT,WALK": Plan(itineraries=[itin(leg("TRAM", "13"), duration=900.0)]),
            "BICYCLE,TRANSIT,WALK": Plan(itineraries=[itin(leg("BUS", "22"), duration=600.0)]),
        }
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    transit = [c for c in candidates if c.kind == "transit"]
    assert len(transit) == 1
    assert transit[0].itinerary.duration == 600.0


async def test_partial_failure_still_returns_other_kinds():
    otp = FakeOTP(
        {"BICYCLE": Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)])},
        fail={"TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"},
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert [c.kind for c in candidates] == ["bike"]


async def test_all_failures_raise():
    otp = FakeOTP({}, fail={"BICYCLE", "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"})
    with pytest.raises(OTPError):
        await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
