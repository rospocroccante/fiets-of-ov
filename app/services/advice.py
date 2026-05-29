"""The rain-aware decision engine — bike vs public transport.

This is the core of the service and, by design, a **pure function**: given a bike
itinerary, a transit itinerary (or none), and a rain forecast, it returns a
recommendation. No network, no clock, no I/O — everything it needs is in its
arguments, which is what keeps it fully unit-testable with fixtures.

The logic, step by step:

1. Work out the cycling window in local wall-clock time. OTP gives absolute epoch-ms
   timestamps; Buienradar gives `HH:MM` local slots. We convert the bike start/end to
   Europe/Amsterdam time-of-day so the two line up. (Europe/Amsterdam is a fixed domain
   constant for an Amsterdam-only service, not configuration.)
2. Look at the rain slots that fall within that window and find the peak intensity.
3. If the ride stays dry (peak below the "is it really raining" threshold) -> bike,
   and mention when rain next arrives, if at all.
4. If rain is expected during the ride:
   - with a transit option -> recommend it, so the rider stays dry;
   - with no transit option -> still recommend bike (there's no alternative), but flag
     the rain so the answer is honest.

The assumption tying OTP and Buienradar together is that the trip departs roughly
"now": OTP plans from the current time and Buienradar forecasts from now, so their
wall-clock times are comparable. Good enough for the MVP; a future version could take
an explicit departure time.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast, RainSlot
from app.clients.otp import Itinerary
from app.schemas.advice import AdviceResponse

# Amsterdam-only service: the local timezone is a domain constant, not config.
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# Below this, precipitation is drizzle-or-nothing and we treat the ride as dry.
# 0.1 mm/h corresponds to Buienradar intensity code 77 (10**((77-109)/32)).
DEFAULT_RAIN_THRESHOLD_MM_H = 0.1


def _local_time(epoch_ms: int) -> time:
    """Convert an OTP epoch-ms timestamp to Amsterdam wall-clock time-of-day."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=LOCAL_TZ).time()


def _minutes(seconds: float) -> int:
    """Round a duration in seconds to whole minutes for human-facing output."""
    return round(seconds / 60)


def _transit_line(transit: Itinerary) -> str:
    """Describe the transit option, e.g. "tram 13"; fall back to "public transport"."""
    for leg in transit.legs:
        # The first non-walking leg is the line the rider actually boards.
        if leg.mode != "WALK" and leg.route_short_name:
            return f"{leg.mode.lower()} {leg.route_short_name}"
    return "public transport"


def decide(
    *,
    bike: Itinerary,
    transit: Itinerary | None,
    rain: RainForecast | None,
    rain_threshold_mm_h: float = DEFAULT_RAIN_THRESHOLD_MM_H,
) -> AdviceResponse:
    """Recommend bike or transit for a trip, given the short-term rain forecast.

    `rain` is None when the forecast is unavailable (Buienradar down and no usable
    cache). We can't assess the weather, so we degrade rather than fail: default to
    bike — the app is bike-first — and say plainly that the forecast is unknown.
    """
    bike_minutes = _minutes(bike.duration)
    transit_minutes = _minutes(transit.duration) if transit is not None else None

    # Step 0: no forecast at all -> honest, bike-first degraded answer. The rain fields
    # are None (not 0.0) so clients can tell "dry" apart from "we don't know".
    if rain is None:
        return AdviceResponse(
            recommendation="bike",
            reason=f"rain forecast unavailable → bike ({bike_minutes} min)",
            bike_minutes=bike_minutes,
            transit_minutes=transit_minutes,
            max_rain_mm_per_h=None,
            rain_expected=None,
        )

    # Step 1: the cycling window, in local time-of-day.
    ride_start = _local_time(bike.start_time)
    ride_end = _local_time(bike.end_time)

    # Step 2: rain slots overlapping the ride, and the peak intensity within it.
    ride_slots = [s for s in rain.slots if ride_start <= s.time <= ride_end]
    wet_slots = [s for s in ride_slots if s.mm_per_h >= rain_threshold_mm_h]
    peak_mm_h = round(max((s.mm_per_h for s in ride_slots), default=0.0), 4)

    # Step 3: dry ride -> bike.
    if not wet_slots:
        next_rain = _first_rain_after(rain, ride_end, rain_threshold_mm_h)
        if next_rain is not None:
            reason = (
                f"dry during your {bike_minutes}-min ride "
                f"(rain only from {next_rain.time:%H:%M}) → bike"
            )
        else:
            reason = f"no rain expected in the next ~2h → bike ({bike_minutes} min)"
        return AdviceResponse(
            recommendation="bike",
            reason=reason,
            bike_minutes=bike_minutes,
            transit_minutes=transit_minutes,
            max_rain_mm_per_h=peak_mm_h,
            rain_expected=False,
        )

    # Step 4: rain during the ride.
    first_wet = wet_slots[0]
    if transit is None:
        # No alternative — recommend bike anyway, but be honest about the rain.
        reason = (
            f"rain expected around {first_wet.time:%H:%M} (~{peak_mm_h:g} mm/h) "
            f"but no public-transport route found → bike ({bike_minutes} min), bring a raincoat"
        )
        return AdviceResponse(
            recommendation="bike",
            reason=reason,
            bike_minutes=bike_minutes,
            transit_minutes=None,
            max_rain_mm_per_h=peak_mm_h,
            rain_expected=True,
        )

    reason = (
        f"rain around {first_wet.time:%H:%M} (~{peak_mm_h:g} mm/h) → "
        f"take {_transit_line(transit)} ({transit_minutes} min)"
    )
    return AdviceResponse(
        recommendation="transit",
        reason=reason,
        bike_minutes=bike_minutes,
        transit_minutes=transit_minutes,
        max_rain_mm_per_h=peak_mm_h,
        rain_expected=True,
    )


def _first_rain_after(rain: RainForecast, after: time, threshold: float) -> RainSlot | None:
    """First slot strictly after `after` that exceeds the rain threshold, if any."""
    for slot in rain.slots:
        if slot.time > after and slot.mm_per_h >= threshold:
            return slot
    return None
