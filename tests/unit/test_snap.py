"""Unit tests for the pedestrian-hub bike snapping fallback."""

from __future__ import annotations

import asyncio

from app.clients.otp import BIKE_MODES, Itinerary, Leg, Plan
from app.services.scoring import classify_kind
from app.services.snap import bike_with_snapping

ORIGIN = (52.3791, 4.9003)
# Bijlmer ArenA: exact coordinate sits on a pedestrian deck with no adjacent bike edge.
DECK = (52.3119, 4.9476)


def _leg(mode: str, to_name: str | None = None) -> Leg:
    return Leg(
        mode=mode,
        start_time=0,
        end_time=600_000,
        duration=600.0,
        distance=1000.0,
        to_name=to_name,
    )


def _bike_plan(to_name: str | None = None) -> Plan:
    return Plan(
        itineraries=[
            Itinerary(
                duration=600.0, start_time=0, end_time=600_000, legs=[_leg("BICYCLE", to_name)]
            )
        ]
    )


class _DeckOTP:
    """OTP stub: the bike query (BIKE_MODES, bike+ferry) routes fine everywhere EXCEPT
    when an endpoint is exactly DECK."""

    def __init__(self) -> None:
        self.bike_calls: list[tuple] = []
        self.modes: list[str] = []
        self.slim_flags: list[bool] = []

    async def plan(
        self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
    ) -> Plan:
        if mode == BIKE_MODES:
            self.bike_calls.append((from_place, to_place))
            self.modes.append(mode)
            self.slim_flags.append(slim)
            if from_place == DECK or to_place == DECK:
                return Plan(itineraries=[])  # on the deck: no bikeable edge
            return _bike_plan()
        return Plan(itineraries=[])


async def test_snaps_destination_off_the_deck():
    otp = _DeckOTP()
    itin = await bike_with_snapping(otp, ORIGIN, DECK)
    assert itin is not None
    assert classify_kind(itin) == "bike"
    # It nudged the destination (never queried the exact DECK as a bikeable target again
    # beyond the offsets); the winning call used a to_place != DECK.
    assert all(to != DECK or frm == ORIGIN for frm, to in otp.bike_calls)
    assert any(to != DECK for _, to in otp.bike_calls)
    # Every probe used the bike+ferry mode so IJ-adjacent snaps can still cross by ferry.
    assert all(m == BIKE_MODES for m in otp.modes)


async def test_snaps_origin_when_origin_is_the_deck():
    otp = _DeckOTP()
    itin = await bike_with_snapping(otp, DECK, ORIGIN)
    assert itin is not None
    assert classify_kind(itin) == "bike"


async def test_returns_none_when_no_offset_is_bikeable():
    class _DeadOTP:
        async def plan(
            self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
        ) -> Plan:
            return Plan(itineraries=[])

    itin = await bike_with_snapping(_DeadOTP(), ORIGIN, DECK)
    assert itin is None


async def test_ring_offsets_are_probed_concurrently():
    # The nearest ring's 8 probes must all be in flight together; ring-by-ring semantics
    # cap concurrency at one ring's worth. The old sequential probing never exceeded 1.
    class _SlowOTP:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def plan(
            self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
        ) -> Plan:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.005)
            self.active -= 1
            return Plan(itineraries=[])

    otp = _SlowOTP()
    await bike_with_snapping(otp, ORIGIN, DECK)
    assert otp.max_active == 8  # the whole nearest ring at once, never more than a ring


async def test_first_offset_in_ring_order_wins_even_when_slower():
    # Every offset in the nearest ring routes, but the ring's FIRST offset answers last:
    # the winner must be picked by the ring's defined offset order, not by answer order.
    first_offset_dest = (DECK[0] + 0.002, DECK[1] + 0.0)

    def _marker(to_place) -> str:
        return f"{to_place[0]:.6f},{to_place[1]:.6f}"

    class _RacingOTP:
        async def plan(
            self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
        ) -> Plan:
            if slim and to_place == first_offset_dest:
                await asyncio.sleep(0.01)  # the nearest offset answers slowest
            return _bike_plan(to_name=_marker(to_place))

    itin = await bike_with_snapping(_RacingOTP(), ORIGIN, DECK)
    assert itin is not None
    assert itin.legs[0].to_name == _marker(first_offset_dest)


async def test_probes_are_slim_and_the_winner_is_replanned_in_full():
    otp = _DeckOTP()
    itin = await bike_with_snapping(otp, ORIGIN, DECK)
    assert itin is not None
    # The fan-out probes are cheap existence checks (slim)...
    assert any(otp.slim_flags)
    # ...and the returned itinerary comes from a final full-detail re-plan.
    assert otp.slim_flags[-1] is False


async def test_snapping_gives_up_at_the_deadline(monkeypatch):
    # A wedged OTP must not hold the request hostage for up to 24 probe queries: past
    # the configured deadline the fallback returns the no-bike answer.
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "snap_timeout_seconds", 0.05)

    class _WedgedOTP:
        async def plan(
            self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
        ) -> Plan:
            await asyncio.sleep(30)
            return Plan(itineraries=[])

    itin = await bike_with_snapping(_WedgedOTP(), ORIGIN, DECK)
    assert itin is None
