"""The rain-aware recommendation engine — bike vs public transport.

This is the core of the service and, by design, a **pure function**: given a list of
routing candidates and a rain forecast, it ranks them and returns the best option with a
human-readable reason. No network, no clock, no I/O — everything it needs is in its
arguments, which is what keeps it fully unit-testable with fixtures.

The logic, step by step:

1. Delegate to `scoring.rank()` which assigns each candidate a generalized cost that
   adds rain penalties to exposed (bike/walk) minutes. Lower cost wins.
2. Compute the rain summary (peak mm/h, wet/dry) over the **pure-bike** candidate's
   window when one exists, so the weather banner and notifier warn-trigger remain
   identical to the old decide() semantics.
3. Build a human-readable reason that adapts to the top candidate's kind and rain state.

The assumption tying OTP and Buienradar together is that the trip departs roughly
"now": OTP plans from the current time and Buienradar forecasts from now, so their
wall-clock times are comparable. Good enough for the MVP; a future version could take
an explicit departure time.
"""

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast
from app.clients.otp import Itinerary
from app.services.scoring import (
    DEFAULT_RAIN_THRESHOLD_MM_H,
    Candidate,
    OptionKind,
    ScoredCandidate,
    rank,
)

# Amsterdam-only service: the local timezone is a domain constant, not config.
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def _local_time(epoch_ms: int) -> time:
    """Convert an OTP epoch-ms timestamp to Amsterdam wall-clock time-of-day."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=LOCAL_TZ).time()


def _minutes(seconds: float) -> int:
    """Round a duration in seconds to whole minutes for human-facing output."""
    return round(seconds / 60)


@dataclass(frozen=True)
class RankedPlan:
    """The ranked options plus the rain summary and reason for the recommended one."""

    options: list[ScoredCandidate]  # best first; options[0] is the recommendation
    recommendation: OptionKind
    reason: str
    max_rain_mm_per_h: float | None
    rain_expected: bool | None
    bike_minutes: int | None
    transit_minutes: int | None


def _rain_summary(
    itinerary: Itinerary, rain: RainForecast | None, threshold: float
) -> tuple[bool | None, float | None]:
    """Peak mm/h and wet/dry over an itinerary's exposed (bike/walk) window.

    Returns (rain_expected, max_rain_mm_per_h). `(None, None)` when the forecast is
    unavailable, so clients can tell "dry" (False, 0.0) from "unknown" (None, None).
    """
    if rain is None:
        return None, None
    start = _local_time(itinerary.start_time)
    end = _local_time(itinerary.end_time)
    window = [s for s in rain.slots if start <= s.time <= end]
    peak = round(max((s.mm_per_h for s in window), default=0.0), 4)
    expected = any(s.mm_per_h >= threshold for s in window)
    return expected, peak


def _boarding_line(itinerary: Itinerary) -> str | None:
    """The first transit line boarded (e.g. "metro 52"), or None for a bike-only trip."""
    for leg in itinerary.legs:
        if leg.mode not in {"WALK", "BICYCLE"} and leg.route_short_name:
            return f"{leg.mode.lower()} {leg.route_short_name}"
    return None


def _bike_handoff_stop(itinerary: Itinerary) -> str | None:
    """Where the rider parks the bike in a bike-and-ride trip (the bike leg's end)."""
    for leg in itinerary.legs:
        if leg.mode == "BICYCLE":
            return leg.to_name
    return None


def _reason(
    top: ScoredCandidate, rain: RainForecast | None, rain_expected: bool | None, peak: float | None
) -> str:
    minutes = _minutes(top.itinerary.duration)
    if rain_expected is None:
        label = {"bike": "bike", "transit": "transit", "bike_and_ride": "bike + transit"}[top.kind]
        return f"rain forecast unavailable -> fastest is {label} ({minutes} min)"
    if not rain_expected:
        if top.kind == "bike":
            return f"dry during your {minutes}-min ride -> bike"
        return f"dry -> fastest is {top.kind.replace('_', ' ')} ({minutes} min)"
    # Rain is expected on the bike window.
    when = f"~{peak:g} mm/h" if peak else "rain"
    if top.kind == "transit":
        line = _boarding_line(top.itinerary) or "public transport"
        return f"rain ({when}) -> take {line} ({minutes} min)"
    if top.kind == "bike_and_ride":
        stop = _bike_handoff_stop(top.itinerary) or "the stop"
        line = _boarding_line(top.itinerary) or "public transport"
        return f"rain ({when}) -> bike to {stop}, then {line} ({minutes} min)"
    return f"rain ({when}) but bike is still fastest -> bike ({minutes} min), bring a raincoat"


def recommend(candidates: list[Candidate], rain: RainForecast | None) -> RankedPlan:
    """Rank candidates by rain-aware cost and build the recommendation + reason."""
    ordered = rank(candidates, rain)
    top = ordered[0]

    bike = next((c for c in ordered if c.kind == "bike"), None)
    transit = next((c for c in ordered if c.kind == "transit"), None)
    summary_itin = bike.itinerary if bike is not None else top.itinerary
    rain_expected, peak = _rain_summary(summary_itin, rain, DEFAULT_RAIN_THRESHOLD_MM_H)

    return RankedPlan(
        options=ordered,
        recommendation=top.kind,
        reason=_reason(top, rain, rain_expected, peak),
        max_rain_mm_per_h=peak,
        rain_expected=rain_expected,
        bike_minutes=_minutes(bike.itinerary.duration) if bike else None,
        transit_minutes=_minutes(transit.itinerary.duration) if transit else None,
    )
