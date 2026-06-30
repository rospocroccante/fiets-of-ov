# Multimodal Rain-Aware Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the binary bike-vs-transit rain switch with a rain-aware generalized-cost ranking over bike, transit, and bike-and-ride (bike+transit+walk) candidates, surfaced as a ranked list of options end-to-end.

**Architecture:** The backend (`fiets-of-ov`) fans out three concurrent OpenTripPlanner queries (`BICYCLE`, `TRANSIT,WALK`, `BICYCLE,TRANSIT,WALK`), pools and classifies the itineraries into one best candidate per kind, scores each with a pure rain-aware cost function, and returns them ranked. The frontend (`fiets-of-ov-frontend`) renders the ranked `options` list (it already has an `OptionView`/`options` abstraction).

**Tech Stack:** Python 3.12 · FastAPI · Pydantic v2 · httpx (async) · pytest + respx + ruff (backend). React 18 · TypeScript · Leaflet · Vitest (frontend).

## Global Constraints

- Backend repo: `/Users/Rospo/Vibecoding/fiets-of-ov` — work on branch `feat/multimodal-rain-aware-routing` (already created).
- Frontend repo: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend` — create branch `feat/multimodal-rain-aware-routing` before Task 8.
- No emoji and NO `Co-Authored-By`/`Generated with` trailers in any commit message (user global rule). Plain text only.
- Backend: every external call stays bounded by the existing per-call timeout; never fabricate a route on failure (raise `OTPError` → 502). Pure functions stay pure (no I/O, no clock).
- Backend timezone is `Europe/Amsterdam` (domain constant, reuse existing `ZoneInfo("Europe/Amsterdam")`).
- Rain threshold constant `DEFAULT_RAIN_THRESHOLD_MM_H = 0.1`.
- Backend lint/test commands: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest` and `ruff check . && ruff format .`.
- Frontend test/build: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npm test` (vitest) and `npm run build` (`tsc --noEmit && vite build`).
- This plan refines the spec's dedup ("retain runners-up") to **one best option per kind** — simpler and fits the frontend's mode-keyed selection. `OptionKind = "bike" | "transit" | "bike_and_ride"`.

---

## File Structure

**Backend — create:**
- `app/services/scoring.py` — pure: kinds, `Candidate`, `Weights`, cost `score()`, `rank()`.
- `app/services/planner.py` — async `gather_candidates()`: 3 parallel OTP queries → best-per-kind.
- `tests/unit/test_scoring.py`, `tests/unit/test_planner.py`.

**Backend — modify:**
- `app/services/advice.py` — replace `decide()` with `recommend()` returning a `RankedPlan`.
- `app/schemas/plan.py` — add `OptionOut`, `options` list; widen `recommendation`.
- `app/schemas/advice.py` — widen `recommendation` literal.
- `app/api/plan.py`, `app/api/advice.py` — call `gather_candidates()` + `recommend()`.
- `app/services/notify.py` — call `gather_candidates()` + `recommend()` (bike still required to warn).
- `tests/unit/test_advice.py`, `tests/unit/test_advice_endpoint.py`, and `tests/unit/test_plan_endpoint.py` (or existing plan endpoint test) — migrate to ranked shape + 3-query respx helper.

**Frontend — modify:**
- `src/api/types.ts` — `Mode += "bike_and_ride"`, add `Option`, `Plan.options`.
- `src/lib/planView.ts` — build `OptionView`s from `plan.options`.
- `src/api/mock.ts` — add bike-and-ride itinerary + `options` arrays.
- `src/components/AdviceCard.tsx` — icon/label for `bike_and_ride`.
- `src/lib/planView.test.ts`, `src/api/mock.test.ts` (and any failing component test) — update to new shape.

**Frontend — verify only (no code change expected):** `src/components/ItineraryDetails.tsx`, `src/components/MapView.tsx`, `src/App.tsx`, `src/hooks/useTripPlan.ts` (already option-list driven).

---

## Task 1: Scoring engine (pure)

**Files:**
- Create: `/Users/Rospo/Vibecoding/fiets-of-ov/app/services/scoring.py`
- Test: `/Users/Rospo/Vibecoding/fiets-of-ov/tests/unit/test_scoring.py`

**Interfaces:**
- Consumes: `app.clients.otp.Itinerary`, `app.clients.otp.Leg`, `app.clients.buienradar.RainForecast`.
- Produces:
  - `OptionKind = Literal["bike", "transit", "bike_and_ride"]`
  - `classify_kind(itinerary: Itinerary) -> OptionKind | None`
  - `Candidate(kind: OptionKind, itinerary: Itinerary)` (frozen dataclass)
  - `ScoredCandidate(kind: OptionKind, itinerary: Itinerary, cost: float, rain_minutes: int)`
  - `Weights` (frozen dataclass) + `DEFAULT_WEIGHTS`
  - `score(candidate, rain, weights=DEFAULT_WEIGHTS, threshold=DEFAULT_RAIN_THRESHOLD_MM_H) -> ScoredCandidate`
  - `rank(candidates: list[Candidate], rain, weights=DEFAULT_WEIGHTS, threshold=...) -> list[ScoredCandidate]` (best first)
  - `DEFAULT_RAIN_THRESHOLD_MM_H = 0.1`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scoring.py
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
        duration=sum(l.duration for l in legs),
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
    ordered = rank(
        [Candidate("bike", BIKE), Candidate("bike_and_ride", BIKE_RIDE)], rain=wet
    )
    assert ordered[0].kind == "bike_and_ride"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/unit/test_scoring.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.scoring'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/scoring.py
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

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast
from app.clients.otp import Itinerary, Leg

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
DEFAULT_RAIN_THRESHOLD_MM_H = 0.1

OptionKind = Literal["bike", "transit", "bike_and_ride"]

# Modes whose rider is exposed to the weather; everything else is sheltered transit.
_EXPOSED_MODES = {"BICYCLE", "WALK"}


def classify_kind(itinerary: Itinerary) -> OptionKind | None:
    """Classify an itinerary by its leg modes, or None for a useless walk-only trip.

    bike: a BICYCLE leg, no transit. transit: a transit leg, no BICYCLE. bike_and_ride:
    both. None: only WALK legs (a walk leaves the rider just as wet as cycling).
    """
    has_bike = any(leg.mode == "BICYCLE" for leg in itinerary.legs)
    has_transit = any(leg.mode not in _EXPOSED_MODES for leg in itinerary.legs)
    if has_bike and has_transit:
        return "bike_and_ride"
    if has_transit:
        return "transit"
    if has_bike:
        return "bike"
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
    transfer_penalty_min: float = 4.0


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate with its computed generalized cost and total rain-exposed minutes."""

    kind: OptionKind
    itinerary: Itinerary
    cost: float
    rain_minutes: int


def _local_time(epoch_ms: int) -> time:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=LOCAL_TZ).time()


def _rain_penalty(peak_mm_h: float, weights: Weights) -> float:
    for bound, penalty in weights.rain_bands:
        if peak_mm_h < bound:
            return penalty
    return weights.heavy_penalty


def _leg_rain(leg: Leg, rain: RainForecast, threshold: float) -> tuple[float, float]:
    """Return (exposed_minutes_in_rain, peak_mm_h) for an exposed leg over its window."""
    start = _local_time(leg.start_time)
    end = _local_time(leg.end_time)
    wet = [s for s in rain.slots if start <= s.time <= end and s.mm_per_h >= threshold]
    if not wet:
        return 0.0, 0.0
    # 5-min slot granularity: each wet slot contributes up to 5 min, capped by leg length.
    leg_minutes = leg.duration / 60
    exposed = min(leg_minutes, 5.0 * len(wet))
    peak = max(s.mm_per_h for s in wet)
    return exposed, peak


def _transfer_count(itinerary: Itinerary) -> int:
    boardings = sum(1 for leg in itinerary.legs if leg.mode not in _EXPOSED_MODES)
    return max(0, boardings - 1)


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
    if rain is not None:
        for leg in itin.legs:
            if leg.mode in _EXPOSED_MODES:
                exposed, peak = _leg_rain(leg, rain, threshold)
                rain_minutes += exposed
                rain_cost += exposed * _rain_penalty(peak, weights)
    transfer_cost = _transfer_count(itin) * weights.transfer_penalty_min
    cost = total_minutes + rain_cost + transfer_cost
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
    """Score all candidates, returning them sorted ascending by cost (best first).

    Ties broken by shorter duration, then kind name, for deterministic ordering.
    """
    scored = [score(c, rain, weights, threshold) for c in candidates]
    scored.sort(key=lambda s: (s.cost, s.itinerary.duration, s.kind))
    return scored
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/unit/test_scoring.py -q && ruff check app/services/scoring.py tests/unit/test_scoring.py`
Expected: PASS (4 tests), no lint errors.

- [ ] **Step 5: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add app/services/scoring.py tests/unit/test_scoring.py
git commit -m "feat: rain-aware generalized-cost scoring for routing candidates"
```

---

## Task 2: Candidate generation (parallel OTP fan-out)

**Files:**
- Create: `/Users/Rospo/Vibecoding/fiets-of-ov/app/services/planner.py`
- Test: `/Users/Rospo/Vibecoding/fiets-of-ov/tests/unit/test_planner.py`

**Interfaces:**
- Consumes: `app.clients.otp.OTPClient`, `OTPError`, `Itinerary`; `app.services.scoring.Candidate`, `classify_kind`, `OptionKind`.
- Produces: `async gather_candidates(otp, from_place, to_place, departure=None) -> list[Candidate]` — one best (shortest) candidate per kind; raises `OTPError` only if every query fails.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_planner.py
import pytest

from app.clients.otp import Itinerary, Leg, OTPError, Plan
from app.services.planner import gather_candidates


def leg(mode: str, route: str | None = None) -> Leg:
    return Leg(
        mode=mode, start_time=0, end_time=600_000, duration=600.0,
        distance=1000.0, route_short_name=route,
    )


def itin(*legs: Leg, duration: float = 600.0) -> Itinerary:
    return Itinerary(duration=duration, start_time=0, end_time=int(duration * 1000), legs=list(legs))


class FakeOTP:
    """Answers plan() by the mode string; modes not in the map raise OTPError."""

    def __init__(self, by_mode: dict[str, Plan], fail: set[str] | None = None):
        self.by_mode = by_mode
        self.fail = fail or set()
        self.calls: list[str] = []

    async def plan(self, *, from_place, to_place, mode, departure=None) -> Plan:
        self.calls.append(mode)
        if mode in self.fail:
            raise OTPError(f"boom {mode}")
        return self.by_mode.get(mode, Plan(itineraries=[]))


@pytest.mark.asyncio
async def test_gathers_one_best_candidate_per_kind():
    otp = FakeOTP(
        {
            "BICYCLE": Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)]),
            "TRANSIT,WALK": Plan(itineraries=[itin(leg("WALK"), leg("TRAM", "13"), duration=720.0)]),
            "BICYCLE,TRANSIT,WALK": Plan(
                itineraries=[itin(leg("BICYCLE"), leg("SUBWAY", "52"), duration=900.0)]
            ),
        }
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    kinds = sorted(c.kind for c in candidates)
    assert kinds == ["bike", "bike_and_ride", "transit"]
    assert set(otp.calls) == {"BICYCLE", "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"}


@pytest.mark.asyncio
async def test_walk_only_itineraries_are_dropped():
    otp = FakeOTP({"TRANSIT,WALK": Plan(itineraries=[itin(leg("WALK"), duration=2400.0)])})
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert candidates == []  # walk-only is no option; nothing else routed


@pytest.mark.asyncio
async def test_keeps_shortest_per_kind_across_queries():
    # Both the transit query and the mixed query yield a transit itinerary; keep the shorter.
    otp = FakeOTP(
        {
            "TRANSIT,WALK": Plan(itineraries=[itin(leg("TRAM", "13"), duration=900.0)]),
            "BICYCLE,TRANSIT,WALK": Plan(itineraries=[itin(leg("BUS", "22"), duration=600.0)]),
        }
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    transit = [c for c in candidates if c.kind == "transit"]
    assert len(transit) == 1
    assert transit[0].itinerary.duration == 600.0


@pytest.mark.asyncio
async def test_partial_failure_still_returns_other_kinds():
    otp = FakeOTP(
        {"BICYCLE": Plan(itineraries=[itin(leg("BICYCLE"), duration=1200.0)])},
        fail={"TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"},
    )
    candidates = await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
    assert [c.kind for c in candidates] == ["bike"]


@pytest.mark.asyncio
async def test_all_failures_raise():
    otp = FakeOTP({}, fail={"BICYCLE", "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK"})
    with pytest.raises(OTPError):
        await gather_candidates(otp, (52.37, 4.89), (52.35, 4.86))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/unit/test_planner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.planner'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/planner.py
"""Candidate generation: ask OTP for bike, transit, and bike-and-ride options at once.

The single source of routing candidates for `/v1/plan`, `/v1/advice`, and the rain-alert
notifier. It fans out three OTP queries concurrently — BICYCLE, TRANSIT+WALK, and
BICYCLE+TRANSIT+WALK — pools every itinerary, classifies each by its leg modes, drops
walk-only trips, and keeps the single shortest itinerary per kind. OTP does the real
multimodal routing; this orchestrates and de-duplicates.

Resilience matches the rest of the service: a failed query is dropped (its kind is simply
absent); only when *every* query fails do we raise `OTPError`, so the caller surfaces a
clear 502 instead of a fabricated route.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.clients.otp import Itinerary, OTPClient, OTPError
from app.services.scoring import Candidate, OptionKind, classify_kind

# The mixed set yields bike-and-ride; the pure sets guarantee a clean bike baseline (the
# rain summary needs it) and a transit option even when the mixed plan omits them.
_MODE_SETS = ("BICYCLE", "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK")


async def gather_candidates(
    otp: OTPClient,
    from_place: tuple[float, float],
    to_place: tuple[float, float],
    departure: datetime | None = None,
) -> list[Candidate]:
    """Return the best candidate per kind for `from_place` -> `to_place`.

    Runs the three OTP queries concurrently. Raises `OTPError` only if *all* fail.
    """
    results = await asyncio.gather(
        *(
            otp.plan(from_place=from_place, to_place=to_place, mode=mode, departure=departure)
            for mode in _MODE_SETS
        ),
        return_exceptions=True,
    )

    plans = [r for r in results if not isinstance(r, BaseException)]
    if not plans:
        first_error = next((r for r in results if isinstance(r, BaseException)), None)
        raise OTPError(f"all OTP queries failed: {first_error}")

    best: dict[OptionKind, Itinerary] = {}
    for plan in plans:
        for itin in plan.itineraries:
            kind = classify_kind(itin)
            if kind is None:
                continue
            current = best.get(kind)
            if current is None or itin.duration < current.duration:
                best[kind] = itin

    return [Candidate(kind=kind, itinerary=itin) for kind, itin in best.items()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/unit/test_planner.py -q && ruff check app/services/planner.py tests/unit/test_planner.py`
Expected: PASS (5 tests), no lint errors.

Note: if `pytest` reports the async tests as skipped/errored with "async def functions are not natively supported", confirm `pyproject.toml` has `asyncio_mode = "auto"` (it does for the existing async tests) and drop the `@pytest.mark.asyncio` decorators to match the repo's existing style. Check an existing async test (e.g. `tests/unit/test_otp.py`) and mirror it.

- [ ] **Step 5: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add app/services/planner.py tests/unit/test_planner.py
git commit -m "feat: gather bike/transit/bike-and-ride candidates from OTP in parallel"
```

---

## Task 3: Recommendation engine — replace `decide()` with `recommend()`

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/app/services/advice.py` (replace `decide()`, keep `LOCAL_TZ`, `_local_time`, `_minutes`, `_transit_line` helpers; reuse where useful)
- Test: `/Users/Rospo/Vibecoding/fiets-of-ov/tests/unit/test_advice.py` (rewrite around `recommend()`)

**Interfaces:**
- Consumes: `app.services.scoring.rank`, `ScoredCandidate`, `Candidate`, `OptionKind`, `DEFAULT_RAIN_THRESHOLD_MM_H`; `app.clients.buienradar.RainForecast`; `app.clients.otp.Itinerary`.
- Produces:
  - `RankedPlan` (frozen dataclass): `options: list[ScoredCandidate]` (best first), `recommendation: OptionKind`, `reason: str`, `max_rain_mm_per_h: float | None`, `rain_expected: bool | None`, `bike_minutes: int | None`, `transit_minutes: int | None`
  - `recommend(candidates: list[Candidate], rain: RainForecast | None) -> RankedPlan`

**Semantics to preserve:** `max_rain_mm_per_h`/`rain_expected` are computed over the **pure-bike** ride window when a bike candidate exists (else the recommended option's exposed window), keeping the weather banner and notifier warn-trigger identical to today. `recommend()` requires at least one candidate (callers guarantee this or 502 before calling).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_advice.py  (replace the file's contents)
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
    return Leg(mode=mode, start_time=a, end_time=b, duration=(b - a) / 1000,
               distance=1000.0, route_short_name=route, to_name="Spaklerweg")


def itin(*legs: Leg) -> Itinerary:
    return Itinerary(duration=sum(l.duration for l in legs),
                     start_time=legs[0].start_time, end_time=legs[-1].end_time, legs=list(legs))


def rain(*wet: int) -> RainForecast:
    slots = [RainSlot(time=time(14, m), intensity=109 if m in wet else 0,
                      mm_per_h=1.0 if m in wet else 0.0) for m in range(0, 60, 5)]
    return RainForecast(slots=slots)


BIKE = Candidate("bike", itin(leg("BICYCLE", (14, 0), (14, 20))))
TRANSIT = Candidate("transit", itin(leg("WALK", (14, 0), (14, 2)), leg("TRAM", (14, 2), (14, 14), "13")))


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/unit/test_advice.py -q`
Expected: FAIL — `ImportError: cannot import name 'recommend' from 'app.services.advice'`.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `app/services/advice.py` below the module docstring. Keep `LOCAL_TZ`, `_local_time`, `_minutes`, `DEFAULT_RAIN_THRESHOLD_MM_H`, and `_transit_line`; remove `decide()` and `_first_rain_after` (no longer used). New code:

```python
from dataclasses import dataclass

from app.services.scoring import (
    Candidate,
    DEFAULT_RAIN_THRESHOLD_MM_H,
    OptionKind,
    ScoredCandidate,
    rank,
)


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
    itinerary, rain, threshold: float
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


def _boarding_line(itinerary) -> str | None:
    """The first transit line boarded (e.g. "metro 52"), or None for a bike-only trip."""
    for leg in itinerary.legs:
        if leg.mode not in {"WALK", "BICYCLE"} and leg.route_short_name:
            return f"{leg.mode.lower()} {leg.route_short_name}"
    return None


def _bike_handoff_stop(itinerary) -> str | None:
    """Where the rider parks the bike in a bike-and-ride trip (the bike leg's end)."""
    for leg in itinerary.legs:
        if leg.mode == "BICYCLE":
            return leg.to_name
    return None


def _reason(top: ScoredCandidate, rain, rain_expected, peak) -> str:
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
        return f"rain ({when}) -> take {_boarding_line(top.itinerary) or 'public transport'} ({minutes} min)"
    if top.kind == "bike_and_ride":
        stop = _bike_handoff_stop(top.itinerary) or "the stop"
        line = _boarding_line(top.itinerary) or "public transport"
        return f"rain ({when}) -> bike to {stop}, then {line} ({minutes} min)"
    return f"rain ({when}) but bike is still fastest -> bike ({minutes} min), bring a raincoat"


def recommend(candidates: list[Candidate], rain) -> RankedPlan:
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
```

Keep the existing `_transit_line` only if still referenced; otherwise remove it (replaced by `_boarding_line`). Run ruff to catch unused imports/functions.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/unit/test_advice.py -q && ruff check app/services/advice.py`
Expected: PASS (4 tests). (Endpoint/notify tests still reference `decide` and will fail to import — fixed in Tasks 5–7. Run only `test_advice.py` here.)

- [ ] **Step 5: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add app/services/advice.py tests/unit/test_advice.py
git commit -m "feat: replace binary decide() with rain-aware recommend() ranking"
```

---

## Task 4: API schemas — ranked options

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/app/schemas/plan.py`
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/app/schemas/advice.py`

**Interfaces:**
- Produces: `OptionKind` (reuse `app.services.scoring.OptionKind` or redeclare as a `Literal` in schemas), `OptionOut`, updated `PlanResponse` with `options: list[OptionOut]` (no `bike`/`transit`), widened `recommendation`. `AdviceResponse.recommendation` widened.

- [ ] **Step 1: Edit `app/schemas/plan.py`**

Replace the `PlanResponse` class and add `OptionOut`. Keep `PlaceOut`, `StepOut`, `LegOut`, `ItineraryOut` unchanged.

```python
from typing import Literal

OptionKind = Literal["bike", "transit", "bike_and_ride"]


class OptionOut(BaseModel):
    """One ranked door-to-door option with its drawable itinerary."""

    kind: OptionKind
    recommended: bool
    score: float  # generalized cost (sort aid; clients may ignore)
    rain_minutes: int  # cycling/walking minutes exposed to rain (0 if dry)
    itinerary: ItineraryOut


class PlanResponse(BaseModel):
    """The recommendation plus the full ranked list of drawable options."""

    recommendation: OptionKind
    reason: str
    max_rain_mm_per_h: float | None
    rain_expected: bool | None
    origin: PlaceOut
    destination: PlaceOut
    options: list[OptionOut]  # ranked, recommended first
```

- [ ] **Step 2: Edit `app/schemas/advice.py`**

Change the `recommendation` field type:

```python
    recommendation: Literal["bike", "transit", "bike_and_ride"]
```

- [ ] **Step 3: Verify it imports**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && python -c "import app.schemas.plan, app.schemas.advice" && ruff check app/schemas/plan.py app/schemas/advice.py`
Expected: no output / exit 0.

- [ ] **Step 4: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add app/schemas/plan.py app/schemas/advice.py
git commit -m "feat: ranked options + bike_and_ride in plan/advice schemas"
```

---

## Task 5: Wire `/v1/plan` and `/v1/advice` to the new pipeline

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/app/api/plan.py`
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/app/api/advice.py`

**Interfaces:**
- Consumes: `app.services.planner.gather_candidates`, `app.services.advice.recommend`, `OptionOut`/`PlanResponse`.
- Produces: `/v1/plan` returns `PlanResponse{options}`; `/v1/advice` returns `AdviceResponse` with possibly `bike_and_ride`.

- [ ] **Step 1: Rewrite `app/api/plan.py` handler**

Keep `_resolve_place`, `_minutes`, `_leg_out`, `_itinerary_out`. Replace the imports of `first_transit_itinerary`/`decide` and the two-call body of `get_plan` with:

```python
from app.services.advice import recommend
from app.services.planner import gather_candidates
from app.schemas.plan import ItineraryOut, LegOut, OptionOut, PlaceOut, PlanResponse, StepOut
# (remove: from app.services.advice import decide; from ...otp import first_transit_itinerary)


@router.get("/v1/plan", response_model=PlanResponse)
async def get_plan(
    origin: str = Query(alias="from", description="Origin: place name or 'lat,lon'"),
    destination: str = Query(alias="to", description="Destination: place name or 'lat,lon'"),
    otp: OTPClient = Depends(get_otp_client),
    rain_service: RainService = Depends(get_rain_service),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
) -> PlanResponse:
    """Return the rain-aware recommendation plus all ranked, drawable options."""
    from_place = await _resolve_place(origin, geocoder)
    to_place = await _resolve_place(destination, geocoder)

    try:
        candidates = await gather_candidates(otp, from_place, to_place)
    except OTPError as exc:
        raise HTTPException(status_code=502, detail="routing upstream unavailable") from exc
    if not candidates:
        raise HTTPException(status_code=502, detail="no route found for this trip")

    rain = await rain_service.get_forecast(lat=from_place[0], lon=from_place[1])
    plan = recommend(candidates, rain)

    return PlanResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
        origin=PlaceOut(lat=from_place[0], lon=from_place[1]),
        destination=PlaceOut(lat=to_place[0], lon=to_place[1]),
        options=[
            OptionOut(
                kind=opt.kind,
                recommended=(i == 0),
                score=opt.cost,
                rain_minutes=opt.rain_minutes,
                itinerary=_itinerary_out(opt.itinerary),
            )
            for i, opt in enumerate(plan.options)
        ],
    )
```

- [ ] **Step 2: Rewrite `app/api/advice.py` handler**

Replace the two-call body + `decide` with `gather_candidates` + `recommend`, building the existing `AdviceResponse` shape:

```python
from app.services.advice import recommend
from app.services.planner import gather_candidates
# (remove: from app.services.advice import decide; first_transit_itinerary import)


@router.get("/v1/advice", response_model=AdviceResponse)
async def get_advice(
    origin: str = Query(alias="from"),
    destination: str = Query(alias="to"),
    otp: OTPClient = Depends(get_otp_client),
    rain_service: RainService = Depends(get_rain_service),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
) -> AdviceResponse:
    """Return a rain-aware bike / transit / bike-and-ride recommendation for the trip."""
    from_place = await _resolve_place(origin, geocoder)
    to_place = await _resolve_place(destination, geocoder)

    try:
        candidates = await gather_candidates(otp, from_place, to_place)
    except OTPError as exc:
        raise HTTPException(status_code=502, detail="routing upstream unavailable") from exc
    if not candidates:
        raise HTTPException(status_code=502, detail="no route found for this trip")

    rain = await rain_service.get_forecast(lat=from_place[0], lon=from_place[1])
    plan = recommend(candidates, rain)

    return AdviceResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        bike_minutes=plan.bike_minutes if plan.bike_minutes is not None else _minutes(
            plan.options[0].itinerary.duration
        ),
        transit_minutes=plan.transit_minutes,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
    )
```

Keep the existing `_resolve_place` and `_minutes` helpers in `advice.py` (verify `_minutes` exists there; if it lived only in the old code, copy the one-liner `round(seconds / 60)`).

- [ ] **Step 3: Quick import check**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && python -c "import app.api.plan, app.api.advice" && ruff check app/api/plan.py app/api/advice.py`
Expected: exit 0 (no unused-import errors — ensure `first_transit_itinerary`/`decide` imports are gone).

- [ ] **Step 4: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add app/api/plan.py app/api/advice.py
git commit -m "feat: serve ranked multimodal options from /v1/plan and /v1/advice"
```

---

## Task 6: Notifier — use the new pipeline, preserve warn semantics

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/app/services/notify.py`
- Test: `/Users/Rospo/Vibecoding/fiets-of-ov/tests/integration/test_notify.py` (adjust stubs if they assert `decide`)

**Interfaces:**
- Consumes: `gather_candidates`, `recommend`, `RankedPlan`.
- Behaviour: `evaluate_trip_alert` returns an `AdviceResponse` (unchanged signature). Warn only when `rain_expected is True`. **Bike still required**: if no bike candidate is present, return None (skip the alert), matching today's "bike is mandatory to assess" rule.

- [ ] **Step 1: Edit `evaluate_trip_alert` in `app/services/notify.py`**

Replace the imports and the body (the two OTP calls + `decide`) with:

```python
from app.services.advice import recommend
from app.services.planner import gather_candidates
# (remove: from app.clients.otp import first_transit_itinerary; from app.services.advice import decide)
# keep: from app.clients.otp import OTPError


async def evaluate_trip_alert(
    alert: TripAlert, now: datetime, otp, rain_service
) -> AdviceResponse | None:
    """Plan `alert`'s trip at its departure and return the recommendation, or None.

    Bike routing is still mandatory: without a bike candidate there is nothing to assess
    against the rain, so we skip the alert (return None).
    """
    departure = datetime.combine(now.date(), alert.departure_time, tzinfo=AMS)
    origin = (alert.origin_lat, alert.origin_lon)
    destination = (alert.dest_lat, alert.dest_lon)

    try:
        candidates = await gather_candidates(otp, origin, destination, departure=departure)
    except OTPError:
        return None
    if not any(c.kind == "bike" for c in candidates):
        return None

    rain = await rain_service.get_forecast(lat=alert.origin_lat, lon=alert.origin_lon)
    plan = recommend(candidates, rain)
    return AdviceResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        bike_minutes=plan.bike_minutes,
        transit_minutes=plan.transit_minutes,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
    )
```

`process_trip_alert` is unchanged: it still keys off `advice.rain_expected is True`. The `Notification.recommendation` column stores `advice.recommendation` — a string column, so `"bike_and_ride"` is accepted with no migration.

- [ ] **Step 2: Run the notify tests**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest tests/integration/test_notify.py -q`
Expected: PASS. If any test stubs an OTP that answers only `BICYCLE`/`TRANSIT,WALK`, update the stub to also answer `BICYCLE,TRANSIT,WALK` (return an empty `Plan(itineraries=[])` is fine). If a test asserted dry-day no-warn, it still holds (dry -> `rain_expected False`).

- [ ] **Step 3: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add app/services/notify.py tests/integration/test_notify.py
git commit -m "feat: notifier uses multimodal pipeline, keeps rain-warn semantics"
```

---

## Task 7: Endpoint tests — 3-query respx + ranked assertions

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov/tests/unit/test_advice_endpoint.py`
- Modify/locate: the plan endpoint test (search: `rg -l "/v1/plan" tests/`); apply the same helper + ranked assertions.

**Interfaces:**
- The OTP respx mock must answer by the **full** mode set, because the mixed query also begins with `BICYCLE`. Replace `_otp_by_mode` (keyed on `modes[0]`) with `_otp_by_modes` (keyed on the comma-joined mode list).

- [ ] **Step 1: Replace the respx helper**

In `tests/unit/test_advice_endpoint.py`, replace `_otp_by_mode` with:

```python
def _otp_by_modes(by_key: dict[str, httpx.Response]):
    """respx side_effect answering by the full requested mode set (comma-joined)."""

    def handler(request: httpx.Request) -> httpx.Response:
        modes = json.loads(request.content)["variables"]["modes"]
        key = ",".join(m["mode"] for m in modes)
        return by_key.get(key, httpx.Response(200, json=_gql([])))

    return handler
```

Add a mixed fixture next to `BIKE_JSON`/`TRANSIT_JSON`:

```python
# A bike-and-ride itinerary the mixed query returns: bike to a stop, then tram.
_BIKE_RIDE_ITIN = {
    "duration": 900,
    "startTime": ms(14, 0),
    "endTime": ms(14, 15),
    "legs": [
        {"mode": "BICYCLE", "startTime": ms(14, 0), "endTime": ms(14, 5), "duration": 300, "distance": 1200.0, "route": None},
        {"mode": "TRAM", "startTime": ms(14, 5), "endTime": ms(14, 15), "duration": 600, "distance": 2400.0, "route": {"shortName": "13"}},
    ],
}
MIXED_JSON = _gql([_BIKE_RIDE_ITIN])
```

- [ ] **Step 2: Update each test's mock setup and assertions**

For every test that called `_otp_by_mode(bike, transit)`, switch to:

```python
respx.post(GQL_URL).mock(
    side_effect=_otp_by_modes(
        {
            "BICYCLE": httpx.Response(200, json=BIKE_JSON),
            "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
            "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
        }
    )
)
```

Update assertions that no longer apply:
- `test_rain_during_ride_returns_transit`: with rain across the bike window, the recommendation should be `transit` or `bike_and_ride` (both keep the rider dry on the long leg). Assert `body["recommendation"] in {"transit", "bike_and_ride"}` and `body["rain_expected"] is True`.
- `test_walk_only_*`: keep — walk-only itineraries are still dropped by `classify_kind`.
- `test_transit_unavailable_still_returns_bike`: make `TRANSIT,WALK` and `BICYCLE,TRANSIT,WALK` return 503 (or empty), `BICYCLE` returns `BIKE_JSON`; assert `recommendation == "bike"` and `transit_minutes is None`.
- `test_bike_routing_failure_returns_502`: make ALL three queries return 503; assert 502 (gather raises only when all fail).
- `test_buienradar_down_degrades_to_bike`: keep; assert `rain_expected is None` and `"unavailable" in reason`.

- [ ] **Step 3: Add a bike-and-ride happy-path test (plan endpoint)**

In the plan endpoint test file, add:

```python
@respx.mock
def test_plan_returns_ranked_options_with_bike_and_ride():
    respx.post(GQL_URL).mock(
        side_effect=_otp_by_modes(
            {
                "BICYCLE": httpx.Response(200, json=BIKE_JSON),
                "TRANSIT,WALK": httpx.Response(200, json=TRANSIT_JSON),
                "BICYCLE,TRANSIT,WALK": httpx.Response(200, json=MIXED_JSON),
            }
        )
    )
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAIN_WET))

    response = TestClient(app).get("/v1/plan", params={"from": "52.37,4.89", "to": "52.35,4.86"})

    assert response.status_code == 200
    body = response.json()
    kinds = {o["kind"] for o in body["options"]}
    assert kinds == {"bike", "transit", "bike_and_ride"}
    assert body["options"][0]["recommended"] is True
    assert body["recommendation"] == body["options"][0]["kind"]
    # ranked by ascending score
    scores = [o["score"] for o in body["options"]]
    assert scores == sorted(scores)
```

- [ ] **Step 4: Run the full backend suite**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov && pytest -q && ruff check . && ruff format --check .`
Expected: ALL tests pass; lint clean. Fix any test that still imports `decide` or `first_transit_itinerary`.

- [ ] **Step 5: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
git add tests/
git commit -m "test: cover 3-query fan-out and ranked multimodal options"
```

---

## Task 8: Frontend types — options + bike_and_ride

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend/src/api/types.ts`

**Interfaces:**
- Produces: `Mode = "bike" | "transit" | "bike_and_ride"`, `Option`, `Plan.options` (removes `Plan.bike`/`Plan.transit`).

- [ ] **Step 0: Create the frontend branch**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend
git checkout -b feat/multimodal-rain-aware-routing
```

- [ ] **Step 1: Edit `src/api/types.ts`**

Change the `Mode` union and the `Plan` interface; add `Option`:

```typescript
export type Mode = "bike" | "transit" | "bike_and_ride";
```

```typescript
export interface Option {
  kind: Mode;
  recommended: boolean;
  score: number;
  rain_minutes: number;
  itinerary: Itinerary;
}

export interface Plan {
  recommendation: Mode;
  reason: string;
  max_rain_mm_per_h: number | null;
  rain_expected: boolean | null;
  origin: PlaceRef;
  destination: PlaceRef;
  options: Option[];
}
```

Remove the old `bike: Itinerary;` and `transit: Itinerary | null;` fields from `Plan`. Leave `Advice` as-is except widening is not needed (its `recommendation` is `Mode`, already updated by the union change).

- [ ] **Step 2: Type-check (expected to surface call sites)**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npx tsc --noEmit`
Expected: errors in `lib/planView.ts`, `api/mock.ts`, `components/AdviceCard.tsx` (references to `plan.bike`/`plan.transit` and the `ICON` record). These are fixed in Tasks 9–11. This step just confirms the surface.

- [ ] **Step 3: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend
git add src/api/types.ts
git commit -m "feat: ranked options + bike_and_ride mode in Plan types"
```

---

## Task 9: Frontend `planView` — build options from the ranked list

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend/src/lib/planView.ts`
- Test: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend/src/lib/planView.test.ts`

**Interfaces:**
- Consumes: `Plan.options`, `Option`.
- Produces: `buildPlanView(plan: Plan): PlanView` mapping each `Option` to an `OptionView` (recommended first, preserving backend order).

- [ ] **Step 1: Update the test first**

Replace `src/lib/planView.test.ts` to assert against the new mock shape (mock updated in Task 10; write the assertions now, they drive both):

```typescript
import { buildPlanView } from "./planView";
import { mockPlanFor } from "../api/mock";

test("recommended option is first; transit summarised by its lines", () => {
  const v = buildPlanView(mockPlanFor("A", "Bijlmer rain"));
  expect(v.options[0].recommended).toBe(true);
  expect(v.options[0].mode).toBe(v.recommendation);
  const transit = v.options.find((o) => o.mode === "transit");
  expect(transit?.summary).toMatch(/Metro 52/);
});

test("bike-and-ride option is summarised with bike + line", () => {
  const v = buildPlanView(mockPlanFor("A", "Zuid mix"));
  const mix = v.options.find((o) => o.mode === "bike_and_ride");
  expect(mix).toBeTruthy();
  expect(mix?.summary.toLowerCase()).toMatch(/bike/);
});

test("bike-only plan yields a single option summarised in km", () => {
  const v = buildPlanView(mockPlanFor("A", "Polder remote"));
  expect(v.options).toHaveLength(1);
  expect(v.options[0].mode).toBe("bike");
  expect(v.options[0].summary).toMatch(/km/);
});

test("carries the rain fields through", () => {
  const v = buildPlanView(mockPlanFor("A", "Unknown spot"));
  expect(v.rainExpected).toBeNull();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npm test -- planView`
Expected: FAIL (compile error / `mockPlanFor` shape mismatch until Task 10, and `buildPlanView` not yet options-driven).

- [ ] **Step 3: Rewrite `src/lib/planView.ts`**

Keep `OptionView`, `PlanView`, `MODE_LABEL`, `transitLabel`, `km`, `transitSummary`. Replace the per-mode builders and `buildPlanView`:

```typescript
import type { Itinerary, Mode, Option, Plan } from "../api/types";

// ... OptionView, PlanView, MODE_LABEL, transitLabel, km, transitSummary unchanged ...

const TITLE: Record<Mode, string> = {
  bike: "By bike",
  transit: "Public transport",
  bike_and_ride: "Bike + transit",
};

function bikeMinutes(it: Itinerary): number {
  return it.legs
    .filter((l) => l.mode === "BICYCLE")
    .reduce((sum, l) => sum + l.minutes, 0);
}

function summarise(kind: Mode, it: Itinerary): string {
  if (kind === "bike") {
    const d = km(it.distance_m);
    return d != null ? `${d} km by bike` : "Bike route";
  }
  if (kind === "transit") return transitSummary(it);
  // bike_and_ride: short bike leg + the transit lines
  return `Bike ${bikeMinutes(it)} min -> ${transitSummary(it)}`;
}

function toOptionView(option: Option): OptionView {
  const it = option.itinerary;
  return {
    mode: option.kind,
    title: TITLE[option.kind],
    minutes: it.minutes,
    distanceKm: option.kind === "bike" ? km(it.distance_m) : null,
    recommended: option.recommended,
    summary: summarise(option.kind, it),
    itinerary: it,
  };
}

export function buildPlanView(plan: Plan): PlanView {
  // Backend returns options ranked, recommended first; preserve that order.
  const options = plan.options.map(toOptionView);
  return {
    recommendation: plan.recommendation,
    reason: plan.reason,
    rainExpected: plan.rain_expected,
    maxRain: plan.max_rain_mm_per_h,
    options,
  };
}
```

- [ ] **Step 4: Run (will pass after Task 10's mock; for now compile-check)**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npx tsc --noEmit src/lib/planView.ts` (or just proceed to Task 10, then run `npm test -- planView`). Commit together with Task 10 if tests depend on the mock.

- [ ] **Step 5: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend
git add src/lib/planView.ts src/lib/planView.test.ts
git commit -m "feat: build view-models from ranked options incl. bike_and_ride"
```

---

## Task 10: Frontend mock — bike-and-ride + options arrays

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend/src/api/mock.ts`
- Test: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend/src/api/mock.test.ts` (update shape assertions)

**Interfaces:**
- Produces: `mockPlanFor(from, to): Plan` returning `options` arrays; new `"mix"`/`"zuid"` case yielding a recommended `bike_and_ride`.

- [ ] **Step 1: Add a bike-and-ride itinerary builder and rewrite `mockPlanFor`**

Add after `mockTransitItin()`:

```typescript
function mockBikeRideItin(): Itinerary {
  const legs: PlanLeg[] = [
    { mode: "BICYCLE", minutes: 6, distance_m: 1300, route: null, route_long_name: null, headsign: null, from: ref("Amsterdam Centraal", 52.3791, 4.9003), to: ref("Weesperplein", 52.3617, 4.9087), geometry: null, start_time: MS, end_time: at(6), steps: [] },
    { mode: "SUBWAY", minutes: 7, distance_m: null, route: "52", route_long_name: "Noord/Zuidlijn", headsign: "Zuid", from: ref("Weesperplein", 52.3617, 4.9087), to: ref("Europaplein", 52.3395, 4.8918), geometry: null, start_time: at(6), end_time: at(13), steps: [] },
    { mode: "WALK", minutes: 4, distance_m: 300, route: null, route_long_name: null, headsign: null, from: ref("Europaplein", 52.3395, 4.8918), to: ref("Vondelpark", 52.358, 4.8686), geometry: null, start_time: at(13), end_time: at(17), steps: [] },
  ];
  return { minutes: 17, distance_m: 1600, start_time: MS, end_time: at(17), legs };
}

function opt(kind: Mode, recommended: boolean, rain_minutes: number, score: number, itinerary: Itinerary): Option {
  return { kind, recommended, rain_minutes, score, itinerary };
}
```

Add `Mode` and `Option` to the import at the top:

```typescript
import type { Advice, Itinerary, Mode, Option, Plan, PlanLeg, Place, Stop } from "./types";
```

Rewrite `mockPlanFor`:

```typescript
export function mockPlanFor(_from: string, to: string): Plan {
  const t = to.toLowerCase();
  const origin = ref("Amsterdam Centraal", 52.3791, 4.9003);
  const destination = ref("Vondelpark", 52.358, 4.8686);
  const base = { origin, destination };

  if (t.includes("rain") || t.includes("regen")) {
    return {
      ...base,
      recommendation: "transit",
      reason: "rain around 15:10 (~1.2 mm/h) -> take tram 1 (29 min)",
      max_rain_mm_per_h: 1.2,
      rain_expected: true,
      options: [
        opt("transit", true, 0, 29, mockTransitItin()),
        opt("bike", false, 22, 46, mockBikeItin()),
      ],
    };
  }
  if (t.includes("mix") || t.includes("zuid")) {
    return {
      ...base,
      recommendation: "bike_and_ride",
      reason: "rain around 15:10 (~1.0 mm/h) -> bike to Weesperplein, then metro 52 (17 min)",
      max_rain_mm_per_h: 1.0,
      rain_expected: true,
      options: [
        opt("bike_and_ride", true, 6, 23, mockBikeRideItin()),
        opt("transit", false, 0, 29, mockTransitItin()),
        opt("bike", false, 24, 48, mockBikeItin()),
      ],
    };
  }
  if (t.includes("remote") || t.includes("polder")) {
    return {
      ...base,
      recommendation: "bike",
      reason: "rain expected but no transit found -> bike (24 min), bring a raincoat",
      max_rain_mm_per_h: 0.8,
      rain_expected: true,
      options: [opt("bike", true, 24, 50, mockBikeItin())],
    };
  }
  if (t.includes("unknown") || t.includes("fog")) {
    return {
      ...base,
      recommendation: "bike",
      reason: "rain forecast unavailable -> fastest is bike (24 min)",
      max_rain_mm_per_h: null,
      rain_expected: null,
      options: [
        opt("bike", true, 0, 24, mockBikeItin()),
        opt("transit", false, 0, 29, mockTransitItin()),
      ],
    };
  }
  return {
    ...base,
    recommendation: "bike",
    reason: "dry during your 24-min ride (rain only from 15:40) -> bike",
    max_rain_mm_per_h: 0.0,
    rain_expected: false,
    options: [
      opt("bike", true, 0, 24, mockBikeItin()),
      opt("transit", false, 0, 29, mockTransitItin()),
    ],
  };
}
```

- [ ] **Step 2: Update `src/api/mock.test.ts`**

Open it and replace any `plan.bike`/`plan.transit` assertions with `plan.options`-based ones, e.g. `expect(plan.options[0].recommended).toBe(true)` and `expect(plan.options.some((o) => o.kind === "transit")).toBe(true)`. Keep `mockAdviceFor` tests as-is (its shape is unchanged).

- [ ] **Step 3: Run planView + mock tests**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npm test -- planView mock`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend
git add src/api/mock.ts src/api/mock.test.ts
git commit -m "feat: mock ranked options incl. bike-and-ride itinerary"
```

---

## Task 11: Frontend components — bike_and_ride label + full suite green

**Files:**
- Modify: `/Users/Rospo/Vibecoding/fiets-of-ov-frontend/src/components/AdviceCard.tsx`
- Verify (likely no change): `src/components/ResultsPanel.tsx`, `src/components/ItineraryDetails.tsx`, `src/App.tsx`, `src/hooks/useTripPlan.ts`
- Test: any failing component test under `src/components/*.test.tsx`

**Interfaces:**
- Consumes: `OptionView.mode` (now includes `bike_and_ride`).

- [ ] **Step 1: Fix the `ICON` record in `AdviceCard.tsx`**

The `Record<OptionView["mode"], string>` now requires a `bike_and_ride` key (tsc errors without it):

```typescript
const ICON: Record<OptionView["mode"], string> = {
  bike: "Bike",
  transit: "Transit",
  bike_and_ride: "Bike + OV",
};
```

- [ ] **Step 2: Type-check the whole frontend**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npx tsc --noEmit`
Expected: exit 0. If `ResultsPanel`/`App`/`ItineraryDetails` raise errors, they will be about the `Mode` union — fix by ensuring `selectedMode` and `o.mode` comparisons accept the wider union (no logic change; the union widening is type-compatible). Do not restructure selection — it is keyed by `mode`, and kinds are unique per options list.

- [ ] **Step 3: Run the full frontend suite + build**

Run: `cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend && npm test && npm run build`
Expected: ALL tests pass; build succeeds. Fix any remaining `*.test.tsx` that constructed a `Plan` with `bike`/`transit` literals — convert to `options`.

- [ ] **Step 4: Commit**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend
git add src/
git commit -m "feat: render bike-and-ride option; frontend suite on ranked options"
```

---

## Task 12: End-to-end verification against live OTP

**Files:** none (manual verification + notes).

- [ ] **Step 1: Bring up the backend + OTP**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov
docker compose -f docker-compose.yml -f docker-compose.otp.yml up -d
# wait for the graph to build/load (first run is slow); then:
curl -s "http://localhost:8000/v1/plan?from=Amsterdam%20Centraal&to=Bijlmer%20ArenA" | python -m json.tool
```

Expected: JSON with an `options` array containing `bike`, `transit`, and (for a cross-town trip) `bike_and_ride`, ranked, `options[0].recommended == true`, `recommendation` matching `options[0].kind`.

- [ ] **Step 2: Run the frontend against live**

```bash
cd /Users/Rospo/Vibecoding/fiets-of-ov-frontend
# .env.local already sets VITE_API_MODE=live; proxy target defaults to localhost:8000
npm run dev
```

Open the app, plan a cross-town Amsterdam trip, and confirm: multiple option cards render, the recommended one is highlighted, selecting the bike-and-ride option draws a route whose bike leg(s) and transit leg(s) are both visible on the map, and the step-by-step panel lists the bike → board → walk legs.

- [ ] **Step 3: Sanity-check rain behaviour**

Confirm that on a dry forecast the fastest option leads, and (using the mock `"... rain"`/`"... mix"` destinations in mock mode, or a real rainy moment) a transit or bike-and-ride option is recommended above pure bike with a rain-aware reason string.

- [ ] **Step 4: Note any MapView legend gap**

If the map legend does not distinguish a bike-and-ride's bike leg from its transit legs, add a one-line legend entry in `MapView.tsx` and commit; otherwise record "no change needed".

---

## Self-Review

**1. Spec coverage:**
- Mixed itineraries (bike-and-ride): Tasks 2 (mixed OTP query), 4 (schema), 5 (endpoints), 9–11 (frontend). ✓
- Rain-aware generalized cost: Task 1 (formula + bands + transfer penalty). ✓
- Ranked list of options: Tasks 3 (rank/recommend), 4 (`options`), 5, 9. ✓
- Resilience / no fabricated route: Task 2 (raise only if all fail), Task 5 (502 mapping). ✓
- `decide()` three call sites updated: Tasks 5 (advice, plan) + 6 (notify). ✓
- Notify warn semantics preserved: Task 6 (bike required, `rain_expected` trigger). ✓
- Tests (scoring, planner, endpoint 3-query, notify, frontend): Tasks 1, 2, 6, 7, 9, 10, 11. ✓
- OV-fiets future-proofing (no impl): contract is generic (`OptionOut`/legs); documented in spec; no task needed now. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. ✓

**3. Type consistency:** `OptionKind`/`Mode` = `bike | transit | bike_and_ride` everywhere (scoring, schemas, types.ts). `gather_candidates` → `list[Candidate]`; `recommend` → `RankedPlan`; `RankedPlan.options: list[ScoredCandidate]`; `OptionOut{kind,recommended,score,rain_minutes,itinerary}` ↔ frontend `Option{kind,recommended,score,rain_minutes,itinerary}`. `score()` field is `cost` internally, serialized as `score` in `OptionOut`. ✓
