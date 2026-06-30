from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast, RainSlot
from app.clients.otp import Itinerary, Leg
from app.services.scoring import (
    Candidate,
    classify_kind,
    rank,
    score,
)

TZ = ZoneInfo("Europe/Amsterdam")


def ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 6, 1, hour, minute, tzinfo=TZ).timestamp() * 1000)


def leg(mode: str, start: tuple[int, int], end: tuple[int, int], route: str | None = None) -> Leg:
    s, e = ms(*start), ms(*end)
    return Leg(
        mode=mode,
        start_time=s,
        end_time=e,
        duration=(e - s) / 1000,
        distance=1000.0,
        route_short_name=route,
    )


def itin(*legs: Leg) -> Itinerary:
    return Itinerary(
        duration=sum(leg.duration for leg in legs),
        start_time=legs[0].start_time,
        end_time=legs[-1].end_time,
        legs=list(legs),
    )


def rain_at(*wet_minutes: int) -> RainForecast:
    # mm/h 1.0 (code 109) at each given minute past 14:00, dry elsewhere across 14:00..14:55.
    slots = []
    for m in range(0, 60, 5):
        mm = 1.0 if m in wet_minutes else 0.0
        slots.append(RainSlot(time=time(14, m), intensity=109 if mm else 0, mm_per_h=mm))
    return RainForecast(slots=slots)


BIKE = itin(leg("BICYCLE", (14, 0), (14, 20)))
TRANSIT = itin(leg("WALK", (14, 0), (14, 2)), leg("TRAM", (14, 2), (14, 14), route="13"))
BIKE_RIDE = itin(
    leg("BICYCLE", (14, 0), (14, 5)),
    leg("SUBWAY", (14, 5), (14, 18), route="52"),
    leg("WALK", (14, 18), (14, 20)),
)


def test_classify_kind_by_legs():
    assert classify_kind(BIKE) == "bike"
    assert classify_kind(TRANSIT) == "transit"
    assert classify_kind(BIKE_RIDE) == "bike_and_ride"
    walk_only = itin(leg("WALK", (14, 0), (14, 30)))
    assert classify_kind(walk_only) is None


def test_dry_ranks_fastest_first():
    # No rain: pure cost = minutes (+transfers). Bike (20) beats transit (14)? transit is faster.
    ordered = rank([Candidate("bike", BIKE), Candidate("transit", TRANSIT)], rain=None)
    assert [s.kind for s in ordered] == ["transit", "bike"]
    assert ordered[0].rain_minutes == 0


def test_heavy_rain_penalizes_long_exposed_bike():
    # Rain across the whole bike ride: a 20-min exposed ride is penalized far above a
    # 14-min sheltered tram, flipping the order.
    wet = rain_at(*range(0, 60, 5))  # wet every slot
    ordered = rank([Candidate("bike", BIKE), Candidate("transit", TRANSIT)], rain=wet)
    assert ordered[0].kind == "transit"
    bike_scored = next(s for s in ordered if s.kind == "bike")
    assert bike_scored.rain_minutes > 0
    assert bike_scored.cost > 20  # base 20 min + rain penalty


def test_partial_rain_favours_bike_and_ride_over_pure_bike():
    # Rain only late in the window: the short 5-min bike leg of bike-and-ride is exposed
    # far less than the 20-min pure-bike ride, so the mix wins.
    wet = rain_at(*range(10, 60, 5))  # wet from 14:10 on
    ordered = rank([Candidate("bike", BIKE), Candidate("bike_and_ride", BIKE_RIDE)], rain=wet)
    assert ordered[0].kind == "bike_and_ride"


def test_score_arithmetic_is_pinned():
    # TRANSIT: 14 min total, one boarding (0 transfers), no rain -> cost == minutes.
    s = score(Candidate("transit", TRANSIT), rain=None)
    assert s.cost == 14.0
    assert s.rain_minutes == 0
