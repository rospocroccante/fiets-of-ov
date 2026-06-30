"""The recommendation engine — pure function, fully exercised with fixtures.

`recommend(candidates, rain)` takes typed candidates plus a rain forecast and returns
a RankedPlan with the best option first and a human-readable reason. No I/O, so it is
exercised entirely with hand-built fixtures here. Times are anchored to a fixed June day
so the epoch-ms itinerary timestamps line up with the wall-clock HH:MM rain slots.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast, RainSlot
from app.clients.otp import Itinerary, Leg
from app.services.advice import recommend
from app.services.scoring import Candidate

TZ = ZoneInfo("Europe/Amsterdam")


def ms(h: int, m: int) -> int:
    return int(datetime(2026, 6, 1, h, m, tzinfo=TZ).timestamp() * 1000)


def leg(mode: str, s: tuple[int, int], e: tuple[int, int], route: str | None = None) -> Leg:
    a, b = ms(*s), ms(*e)
    return Leg(
        mode=mode,
        start_time=a,
        end_time=b,
        duration=(b - a) / 1000,
        distance=1000.0,
        route_short_name=route,
        to_name="Spaklerweg",
    )


def itin(*legs: Leg) -> Itinerary:
    return Itinerary(
        duration=sum(lg.duration for lg in legs),
        start_time=legs[0].start_time,
        end_time=legs[-1].end_time,
        legs=list(legs),
    )


def rain(*wet: int) -> RainForecast:
    slots = [
        RainSlot(
            time=time(14, m), intensity=109 if m in wet else 0, mm_per_h=1.0 if m in wet else 0.0
        )
        for m in range(0, 60, 5)
    ]
    return RainForecast(slots=slots)


BIKE = Candidate("bike", itin(leg("BICYCLE", (14, 0), (14, 20))))
TRANSIT = Candidate(
    "transit", itin(leg("WALK", (14, 0), (14, 2)), leg("TRAM", (14, 2), (14, 14), "13"))
)


def test_dry_recommends_fastest_and_reports_dry():
    plan = recommend([BIKE, TRANSIT], rain=rain())  # all dry
    assert plan.rain_expected is False
    assert plan.max_rain_mm_per_h == 0.0
    assert plan.options[0].kind == plan.recommendation
    assert plan.bike_minutes == 20
    assert plan.transit_minutes == 14


def test_rain_on_ride_flips_to_transit_with_line_in_reason():
    plan = recommend([BIKE, TRANSIT], rain=rain(*range(0, 60, 5)))
    assert plan.recommendation == "transit"
    assert plan.rain_expected is True
    assert "13" in plan.reason


def test_no_forecast_degrades_to_fastest_and_flags_unknown():
    plan = recommend([BIKE, TRANSIT], rain=None)
    assert plan.rain_expected is None
    assert plan.max_rain_mm_per_h is None
    assert "unavailable" in plan.reason.lower()


def test_bike_only_recommends_bike_with_raincoat_when_wet():
    plan = recommend([BIKE], rain=rain(*range(0, 60, 5)))
    assert plan.recommendation == "bike"
    assert plan.rain_expected is True
    assert plan.transit_minutes is None
