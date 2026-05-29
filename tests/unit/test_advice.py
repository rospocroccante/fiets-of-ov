"""The decision engine — the heart of the service, and a pure function.

`decide(bike, transit, rain)` takes typed itineraries plus a rain forecast and returns
a bike-vs-OV recommendation with a human-readable reason. No I/O, so it is exercised
entirely with hand-built fixtures here. Times are anchored to a fixed June day so the
epoch-ms itinerary timestamps line up with the wall-clock `HH:MM` rain slots.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast, RainSlot
from app.clients.otp import Itinerary, Leg
from app.services.advice import decide

TZ = ZoneInfo("Europe/Amsterdam")


def ms(hour: int, minute: int) -> int:
    """Epoch milliseconds for a local time-of-day on a fixed (CEST) date."""
    return int(datetime(2026, 6, 1, hour, minute, tzinfo=TZ).timestamp() * 1000)


def bike_itinerary(start=(14, 0), end=(14, 20)) -> Itinerary:
    return Itinerary(
        duration=(end[0] * 60 + end[1] - start[0] * 60 - start[1]) * 60,
        start_time=ms(*start),
        end_time=ms(*end),
        legs=[
            Leg(
                mode="BICYCLE",
                start_time=ms(*start),
                end_time=ms(*end),
                duration=1200,
                distance=3000.0,
            )
        ],
    )


def tram_itinerary() -> Itinerary:
    """A 12-minute trip: short walk, then tram 13."""
    return Itinerary(
        duration=720,
        start_time=ms(14, 0),
        end_time=ms(14, 12),
        legs=[
            Leg(
                mode="WALK", start_time=ms(14, 0), end_time=ms(14, 2), duration=120, distance=150.0
            ),
            Leg(
                mode="TRAM",
                start_time=ms(14, 2),
                end_time=ms(14, 12),
                duration=600,
                distance=2400.0,
                route_short_name="13",
            ),
        ],
    )


def forecast(*slots: tuple[int, int, float]) -> RainForecast:
    """Build a forecast from (hour, minute, mm_per_h) tuples."""
    return RainForecast(
        slots=[RainSlot(time=time(h, m), intensity=0, mm_per_h=mm) for (h, m, mm) in slots]
    )


def test_dry_ride_recommends_bike():
    # Dry through the 14:00-14:20 ride; rain only later at 15:00.
    rain = forecast((14, 0, 0.0), (14, 5, 0.0), (14, 10, 0.0), (14, 20, 0.0), (15, 0, 1.0))

    advice = decide(bike=bike_itinerary(), transit=tram_itinerary(), rain=rain)

    assert advice.recommendation == "bike"
    assert advice.rain_expected is False
    assert advice.bike_minutes == 20
    assert advice.max_rain_mm_per_h == 0.0


def test_no_rain_at_all_recommends_bike():
    rain = forecast((14, 0, 0.0), (14, 10, 0.0), (15, 0, 0.0))

    advice = decide(bike=bike_itinerary(), transit=tram_itinerary(), rain=rain)

    assert advice.recommendation == "bike"
    assert advice.rain_expected is False


def test_rain_during_ride_recommends_transit():
    # 1.2 mm/h at 14:05, squarely inside the ride window -> stay dry, take the tram.
    rain = forecast((14, 0, 0.0), (14, 5, 1.2), (14, 10, 0.6))

    advice = decide(bike=bike_itinerary(), transit=tram_itinerary(), rain=rain)

    assert advice.recommendation == "transit"
    assert advice.rain_expected is True
    assert advice.transit_minutes == 12
    assert advice.max_rain_mm_per_h == 1.2
    assert "tram 13" in advice.reason.lower()


def test_rain_but_no_transit_recommends_bike_with_warning():
    rain = forecast((14, 5, 1.2))

    advice = decide(bike=bike_itinerary(), transit=None, rain=rain)

    assert advice.recommendation == "bike"
    assert advice.rain_expected is True
    assert advice.transit_minutes is None
    # No alternative exists, so it must still pick bike but flag the rain.
    assert "rain" in advice.reason.lower()


def test_no_rain_data_degrades_to_bike():
    # Buienradar unavailable -> rain is None. The engine must still answer: default to
    # bike (the app is bike-first) and be honest that the forecast is unknown, rather
    # than crash or invent a forecast.
    advice = decide(bike=bike_itinerary(), transit=tram_itinerary(), rain=None)

    assert advice.recommendation == "bike"
    assert advice.rain_expected is None
    assert advice.max_rain_mm_per_h is None
    assert advice.bike_minutes == 20
    assert advice.transit_minutes == 12
    assert "unavailable" in advice.reason.lower()
