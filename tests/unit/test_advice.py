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
    # A forecast we actually read: the answer is weather-informed, degraded is False.
    assert plan.forecast_degraded is False


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
    assert plan.forecast_degraded is True


def test_degraded_forecast_is_distinguishable_from_a_dry_day():
    # The failure this guards against: with no forecast, scoring applies no rain penalty, so
    # every candidate's cost and rain_minutes come out identical to a genuinely dry day. The
    # explicit flag is the only thing that tells the two apart — assert both that the numbers
    # really do coincide and that the flag separates them.
    degraded = recommend([BIKE, TRANSIT], rain=None)
    dry = recommend([BIKE, TRANSIT], rain=rain())

    assert [o.cost for o in degraded.options] == [o.cost for o in dry.options]
    assert [o.rain_minutes for o in degraded.options] == [0, 0]
    assert [o.rain_minutes for o in dry.options] == [0, 0]
    assert (degraded.forecast_degraded, dry.forecast_degraded) == (True, False)


def test_rain_summary_wraps_past_midnight():
    # The banner summary matches forecast slots to the bike window by local time-of-day;
    # a window spanning midnight (23:50 -> 00:10) has start > end, and the naive match
    # would report a wet 00:05 slot as dry.
    start_ms = int(datetime(2026, 6, 1, 23, 50, tzinfo=TZ).timestamp() * 1000)
    end_ms = int(datetime(2026, 6, 2, 0, 10, tzinfo=TZ).timestamp() * 1000)
    night_leg = Leg(
        mode="BICYCLE",
        start_time=start_ms,
        end_time=end_ms,
        duration=(end_ms - start_ms) / 1000,
        distance=1000.0,
        to_name="Home",
    )
    night_bike = Candidate(
        "bike",
        Itinerary(
            duration=night_leg.duration, start_time=start_ms, end_time=end_ms, legs=[night_leg]
        ),
    )
    forecast = RainForecast(
        slots=[
            RainSlot(time=time(23, 55), intensity=0, mm_per_h=0.0),
            RainSlot(time=time(0, 5), intensity=109, mm_per_h=1.0),
        ]
    )
    plan = recommend([night_bike], rain=forecast)
    assert plan.rain_expected is True
    assert plan.max_rain_mm_per_h == 1.0


def test_bike_only_recommends_bike_with_raincoat_when_wet():
    plan = recommend([BIKE], rain=rain(*range(0, 60, 5)))
    assert plan.recommendation == "bike"
    assert plan.rain_expected is True
    assert plan.transit_minutes is None
