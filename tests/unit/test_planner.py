import logging

import pytest

from app.clients.otp import BIKE_MODES, Itinerary, Leg, OTPError, Plan
from app.core.cache import InMemoryCache
from app.services.planner import gather_candidates, gather_candidates_cached


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

    async def plan(
        self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
    ) -> Plan:
        self.calls.append(mode)
        if mode in self.fail:
            raise OTPError(f"boom {mode}")
        return self.by_mode.get(mode, Plan(itineraries=[]))


async def test_gathers_one_best_candidate_per_kind():
    otp = FakeOTP(
        {
            BIKE_MODES: Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)]),
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
    assert set(otp.calls) == {BIKE_MODES, "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"}


async def test_bike_query_includes_ferry():
    # Pure-bike trips must be able to use GVB ferries for IJ crossings.
    fake = FakeOTP({})
    await gather_candidates(fake, (52.37, 4.89), (52.35, 4.86))
    assert BIKE_MODES == "BICYCLE,FERRY"
    assert BIKE_MODES in fake.calls


async def test_walk_only_itineraries_are_dropped():
    otp = FakeOTP({"TRANSIT,WALK": Plan(itineraries=[itin(leg("WALK"), duration=2400.0)])})
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert candidates == []  # walk-only is no option; nothing else routed


async def test_returns_all_transit_candidates_across_queries():
    # Both the transit query and the mixed query yield a transit itinerary; the planner now
    # returns BOTH (per-kind selection moved to scoring.rank).
    otp = FakeOTP(
        {
            "TRANSIT,WALK": Plan(itineraries=[itin(leg("TRAM", "13"), duration=900.0)]),
            "BICYCLE,TRANSIT,WALK": Plan(itineraries=[itin(leg("BUS", "22"), duration=600.0)]),
        }
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    transit = [c for c in candidates if c.kind == "transit"]
    assert len(transit) == 2
    assert sorted(c.itinerary.duration for c in transit) == [600.0, 900.0]


async def test_partial_failure_still_returns_other_kinds():
    otp = FakeOTP(
        {BIKE_MODES: Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)])},
        fail={"TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"},
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert [c.kind for c in candidates] == ["bike"]


async def test_all_failures_raise():
    otp = FakeOTP({}, fail={BIKE_MODES, "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"})
    with pytest.raises(OTPError):
        await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))


async def test_non_otp_error_from_a_query_propagates():
    # A bug (TypeError etc.) in one query must surface, not masquerade as "no transit".
    class _BuggyOTP:
        async def plan(
            self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
        ) -> Plan:
            if mode == BIKE_MODES:
                raise TypeError("programming error")
            return Plan(itineraries=[])

    with pytest.raises(TypeError):
        await gather_candidates(_BuggyOTP(), (52.37, 4.89), (52.35, 4.86))


async def test_dropped_mode_set_failure_is_logged(caplog):
    # A dropped OTPError must leave a trace: "transit silently missing" should be
    # diagnosable from the logs, naming the mode set that failed.
    otp = FakeOTP(
        {BIKE_MODES: Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)])},
        fail={"TRANSIT,WALK"},
    )
    with caplog.at_level(logging.WARNING, logger="app.services.planner"):
        await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert any("TRANSIT,WALK" in record.message for record in caplog.records)


# --- pedestrian-deck bike snapping fallback ---

_DECK = (52.3119, 4.9476)  # Bijlmer ArenA: on a pedestrian deck, no adjacent bike edge
_ORIGIN = (52.3791, 4.9003)


class _DeckOTP:
    """Bike+ferry routes everywhere except when an endpoint is exactly _DECK; transit routes."""

    def __init__(self):
        self.bike_targets = []

    async def plan(
        self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
    ):
        if mode == BIKE_MODES:
            self.bike_targets.append((from_place, to_place))
            if from_place == _DECK or to_place == _DECK:
                return Plan(itineraries=[])
            return Plan(itineraries=[itin(leg("BICYCLE"), duration=2940.0)])
        if mode == "TRANSIT,WALK":
            return Plan(itineraries=[itin(leg("WALK"), leg("SUBWAY", "54"), duration=1680.0)])
        return Plan(itineraries=[])  # mixed: none from the deck


async def test_snap_fallback_recovers_bike_at_pedestrian_hub():
    otp = _DeckOTP()
    candidates = await gather_candidates(otp, _ORIGIN, _DECK)
    kinds = sorted(c.kind for c in candidates)
    assert kinds == ["bike", "transit"]  # bike recovered via snapping


async def test_no_snap_when_direct_bike_exists():
    # Ordinary trip: direct bike+ferry routes, so the fallback must not fire (only the 3
    # fan-out queries — here 1 mode set uses bike+ferry alone + the mixed set).
    otp = _DeckOTP()
    good_dest = (52.358, 4.8686)
    await gather_candidates(otp, _ORIGIN, good_dest)
    # No snapping ring probes: the only bike+ferry call is the single fan-out query.
    assert otp.bike_targets == [(_ORIGIN, good_dest)]


# --- short-TTL routing cache ---


def _bike_only_otp() -> FakeOTP:
    return FakeOTP({BIKE_MODES: Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)])})


async def test_cached_second_call_skips_otp():
    otp = _bike_only_otp()
    cache = InMemoryCache()

    first = await gather_candidates_cached(otp, cache, (52.37, 4.89), (52.35, 4.86))
    calls_after_first = len(otp.calls)
    second = await gather_candidates_cached(otp, cache, (52.37, 4.89), (52.35, 4.86))

    assert len(otp.calls) == calls_after_first  # served from cache: no new OTP queries
    assert [c.kind for c in second] == [c.kind for c in first] == ["bike"]
    assert second[0].itinerary.duration == first[0].itinerary.duration


async def test_cache_key_rounds_coordinates_to_four_decimals():
    otp = _bike_only_otp()
    cache = InMemoryCache()

    await gather_candidates_cached(otp, cache, (52.37, 4.89), (52.35, 4.86))
    calls = len(otp.calls)
    # ~4 cm away: rounds onto the same 4-decimal key, so the cached plan is reused.
    await gather_candidates_cached(otp, cache, (52.370004, 4.890004), (52.35, 4.86))

    assert len(otp.calls) == calls


async def test_empty_results_are_not_cached():
    otp = FakeOTP({})  # nothing routes anywhere
    cache = InMemoryCache()

    assert await gather_candidates_cached(otp, cache, (52.37, 4.89), (52.35, 4.86)) == []
    calls = len(otp.calls)
    await gather_candidates_cached(otp, cache, (52.37, 4.89), (52.35, 4.86))

    assert len(otp.calls) > calls  # a no-route answer is retried, never pinned for the TTL


async def test_plan_cache_fails_open():
    class _BrokenCache:
        async def get(self, key: str) -> str | None:
            raise RuntimeError("redis down")

        async def set(self, key: str, value: str, ttl_seconds: int) -> None:
            raise RuntimeError("redis down")

    otp = _bike_only_otp()
    candidates = await gather_candidates_cached(otp, _BrokenCache(), (52.37, 4.89), (52.35, 4.86))
    assert [c.kind for c in candidates] == ["bike"]
