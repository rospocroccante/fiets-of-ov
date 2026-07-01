"""Unit tests for the pedestrian-hub bike snapping fallback."""

from __future__ import annotations

from app.clients.otp import Itinerary, Leg, Plan
from app.services.scoring import classify_kind
from app.services.snap import bike_with_snapping

ORIGIN = (52.3791, 4.9003)
# Bijlmer ArenA: exact coordinate sits on a pedestrian deck with no adjacent bike edge.
DECK = (52.3119, 4.9476)


def _leg(mode: str) -> Leg:
    return Leg(mode=mode, start_time=0, end_time=600_000, duration=600.0, distance=1000.0)


def _bike_plan() -> Plan:
    return Plan(
        itineraries=[
            Itinerary(duration=600.0, start_time=0, end_time=600_000, legs=[_leg("BICYCLE")])
        ]
    )


class _DeckOTP:
    """OTP stub: BICYCLE routes fine everywhere EXCEPT when an endpoint is exactly DECK."""

    def __init__(self) -> None:
        self.bike_calls: list[tuple] = []

    async def plan(self, *, from_place, to_place, mode, departure=None) -> Plan:
        if mode == "BICYCLE":
            self.bike_calls.append((from_place, to_place))
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


async def test_snaps_origin_when_origin_is_the_deck():
    otp = _DeckOTP()
    itin = await bike_with_snapping(otp, DECK, ORIGIN)
    assert itin is not None
    assert classify_kind(itin) == "bike"


async def test_returns_none_when_no_offset_is_bikeable():
    class _DeadOTP:
        async def plan(self, *, from_place, to_place, mode, departure=None) -> Plan:
            return Plan(itineraries=[])

    itin = await bike_with_snapping(_DeadOTP(), ORIGIN, DECK)
    assert itin is None
