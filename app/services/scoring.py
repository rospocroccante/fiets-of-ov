"""Rain-aware generalized-cost scoring for routing candidates (pure).

Given candidate itineraries (bike, transit, bike-and-ride) and the short-term rain
forecast, assign each a generalized cost and rank them. Lower cost wins. The cost adds a
rain penalty to every minute spent cycling or walking in the rain, so a sheltered transit
option overtakes a long exposed bike ride exactly when the weather makes cycling
unpleasant — the rain-awareness that is the product's identity, expressed as a real
optimization rather than an on/off switch.

Pure: no I/O, no clock. Everything is derived from the arguments, so it is fully
unit-testable with fixtures (mirroring the discipline of the old `decide()`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast
from app.clients.otp import Itinerary, Leg
from app.services import amsterdam

# Amsterdam-only service: the local timezone is a domain constant, not config. Owned
# here and imported by app/services/advice.py so both modules read the same clock.
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
DEFAULT_RAIN_THRESHOLD_MM_H = 0.1

OptionKind = Literal["bike", "transit", "bike_and_ride"]

# Modes whose rider is exposed to the weather; everything else is sheltered transit.
_EXPOSED_MODES = {"BICYCLE", "WALK"}


def classify_kind(itinerary: Itinerary) -> OptionKind | None:
    """Classify an itinerary by its leg modes, or None for a useless walk-only trip.

    bike: BICYCLE legs with no transit beyond FERRY (a cyclist rolls the bike onto the
    free GVB ferry, so bike+ferry is still a bike trip). transit: a transit leg, no
    BICYCLE. bike_and_ride: BICYCLE plus non-ferry transit. None: only WALK legs (a walk
    leaves the rider just as wet as cycling).
    """
    has_bike = any(leg.mode == "BICYCLE" for leg in itinerary.legs)
    transit_modes = {leg.mode for leg in itinerary.legs} - _EXPOSED_MODES
    if has_bike and transit_modes - {"FERRY"}:
        return "bike_and_ride"
    if has_bike:
        return "bike"
    if transit_modes:
        return "transit"
    return None


@dataclass(frozen=True)
class Candidate:
    """A routing option: its kind and the underlying OTP itinerary."""

    kind: OptionKind
    itinerary: Itinerary


@dataclass(frozen=True)
class Weights:
    """Tunable cost weights.

    `rain_bands` is ordered `(upper_mm_h_bound, penalty_per_exposed_minute)`; the first
    band whose bound the peak intensity is strictly below applies. Above the last bound,
    `heavy_penalty` applies.
    """

    rain_bands: tuple[tuple[float, float], ...] = (
        (0.1, 0.0),  # < 0.1 mm/h: dry
        (0.5, 0.5),  # drizzle
        (1.5, 1.5),  # light
        (4.0, 3.0),  # moderate
    )
    heavy_penalty: float = 6.0
    # Cost of an ordinary transfer; a transfer at a major hub costs less (better
    # cross-platform interchange, higher service frequency).
    transfer_penalty_min: float = 5.0
    hub_transfer_penalty_min: float = 2.0
    # Bonuses (subtracted from cost) rewarding curated Amsterdam patterns.
    ferry_bonus_min: float = 3.0
    station_access_bonus_min: float = 2.0
    # Flat handicap on non-bike options so bike wins unless transit saves more than this
    # many minutes.
    transit_bias_min: float = 10.0
    # Cost per minute of departing later than the kind's earliest candidate: waiting
    # for a sailing is trip time, so the 16 km land loop around the IJ can never beat
    # "wait 10 min for the ferry" — while the card still shows the winner's own ride
    # time plus a "Leave at HH:MM" label, keeping the displayed number low.
    depart_delay_factor: float = 1.0
    # Safety valve: candidates departing further than this beyond their kind's earliest
    # option never compete, however fast, so the card never advertises a departure the
    # user must loiter half an hour for. Per kind, so a mode with sparse service is not
    # wiped from the options.
    max_depart_wait_min: float = 30.0


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate with its computed generalized cost and total rain-exposed minutes."""

    kind: OptionKind
    itinerary: Itinerary
    cost: float
    rain_minutes: int


def local_time(epoch_ms: int) -> time:
    """Convert an OTP epoch-ms timestamp to Amsterdam wall-clock time-of-day.

    Public (like `in_time_window`) because advice.py matches forecast slots against
    itinerary windows with the same conversion.
    """
    return datetime.fromtimestamp(epoch_ms / 1000, tz=LOCAL_TZ).time()


def in_time_window(t: time, start: time, end: time) -> bool:
    """True when time-of-day `t` falls in [start, end] (inclusive), wrapping midnight.

    Forecast slots carry only a wall-clock time, so legs are matched to them by naive
    time-of-day. A window spanning midnight (e.g. 23:50 -> 00:10) has start > end, and a
    plain `start <= t <= end` would match nothing — scoring a wet night ride as dry.
    Such a window instead matches times on either side of the wrap.
    """
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def _rain_penalty(peak_mm_h: float, weights: Weights) -> float:
    for bound, penalty in weights.rain_bands:
        if peak_mm_h < bound:
            return penalty
    return weights.heavy_penalty


def _leg_rain(leg: Leg, rain: RainForecast, threshold: float) -> tuple[float, float]:
    """Return (exposed_minutes_in_rain, peak_mm_h) for an exposed leg over its window."""
    start = local_time(leg.start_time)
    end = local_time(leg.end_time)
    wet = [s for s in rain.slots if in_time_window(s.time, start, end) and s.mm_per_h >= threshold]
    if not wet:
        return 0.0, 0.0
    # 5-min slot granularity: each wet slot contributes up to 5 min, capped by leg length.
    leg_minutes = leg.duration / 60
    exposed = min(leg_minutes, 5.0 * len(wet))
    peak = max(s.mm_per_h for s in wet)
    return exposed, peak


def score(
    candidate: Candidate,
    rain: RainForecast | None,
    weights: Weights = DEFAULT_WEIGHTS,
    threshold: float = DEFAULT_RAIN_THRESHOLD_MM_H,
) -> ScoredCandidate:
    """Generalized cost (minutes-equivalent) for a candidate. Lower is better."""
    itin = candidate.itinerary
    total_minutes = itin.duration / 60
    rain_cost = 0.0
    rain_minutes = 0.0
    # No forecast means no rain penalty, so an unavailable Buienradar produces exactly the
    # same numbers as a clear afternoon. That is the only sane fallback here (inventing a
    # penalty would be worse than admitting ignorance), but it makes the output ambiguous on
    # its own — which is why `recommend()` carries a separate `forecast_degraded` flag out to
    # the API. Do not read a 0 `rain_minutes` as "dry" without checking it.
    if rain is not None:
        for leg in itin.legs:
            if leg.mode in _EXPOSED_MODES:
                exposed, peak = _leg_rain(leg, rain, threshold)
                rain_minutes += exposed
                rain_cost += exposed * _rain_penalty(peak, weights)
    transfer_cost = sum(
        weights.hub_transfer_penalty_min
        if amsterdam.is_near_hub(lat, lon)
        else weights.transfer_penalty_min
        for (lat, lon) in amsterdam.transfer_points(itin)
    )
    ferry_bonus = weights.ferry_bonus_min if amsterdam.has_ferry(itin) else 0.0
    station_bonus = (
        weights.station_access_bonus_min
        if candidate.kind == "bike_and_ride"
        and amsterdam.is_near_hub(*(amsterdam.bike_handoff_point(itin) or (None, None)))
        else 0.0
    )
    bias = weights.transit_bias_min if candidate.kind != "bike" else 0.0
    cost = total_minutes + rain_cost + transfer_cost + bias - ferry_bonus - station_bonus
    return ScoredCandidate(
        kind=candidate.kind,
        itinerary=itin,
        cost=round(cost, 4),
        rain_minutes=round(rain_minutes),
    )


def rank(
    candidates: list[Candidate],
    rain: RainForecast | None,
    weights: Weights = DEFAULT_WEIGHTS,
    threshold: float = DEFAULT_RAIN_THRESHOLD_MM_H,
) -> list[ScoredCandidate]:
    """Score all candidates, keep the lowest-cost one per kind, sorted best first.

    This is the single per-kind selection point: candidates may include several of the same
    kind (the planner returns every classified itinerary), and only the cheapest per kind
    survives — the comparison is strict (<), so on an exact cost tie the first-encountered
    candidate of that kind is kept. The final winners (one per kind) are then sorted by
    cost, with ties broken by shorter duration, then kind name, for deterministic ordering.

    Departure handling: OTP's search window returns itineraries leaving up to ~60 min
    apart. Within each kind, only candidates departing within `weights.max_depart_wait_min`
    (default 30) of that kind's earliest departure compete — the fastest sailing is no use
    if it leaves in half an hour. Surviving candidates have their waited minutes priced
    into cost via `weights.depart_delay_factor` (default 1.0: a minute spent waiting costs
    as much as a minute riding), while the card still shows the winner's own ride time.
    """
    if not candidates:
        return []
    earliest_ms: dict[OptionKind, int] = {}
    for candidate in candidates:
        start = candidate.itinerary.start_time
        prior = earliest_ms.get(candidate.kind)
        earliest_ms[candidate.kind] = start if prior is None else min(prior, start)
    best: dict[OptionKind, ScoredCandidate] = {}
    for candidate in candidates:
        delay_min = (candidate.itinerary.start_time - earliest_ms[candidate.kind]) / 60000
        if delay_min > weights.max_depart_wait_min:
            continue
        scored = score(candidate, rain, weights, threshold)
        if delay_min:
            scored = replace(
                scored, cost=round(scored.cost + delay_min * weights.depart_delay_factor, 4)
            )
        current = best.get(scored.kind)
        if current is None or scored.cost < current.cost:
            best[scored.kind] = scored
    winners = list(best.values())
    winners.sort(key=lambda s: (s.cost, s.itinerary.duration, s.kind))
    return winners
