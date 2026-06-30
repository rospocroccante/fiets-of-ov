# Multimodal, rain-aware routing — design

Date: 2026-06-30
Status: approved (brainstorming) — pending spec review
Repos: `fiets-of-ov` (backend, this repo) + `fiets-of-ov-frontend` (client)

## Problem

Two complaints about the current routing:

1. **Pathfinding is not the best.** The engine returns good itineraries but the app
   throws most away: it uses only `itineraries[0]` from each OTP call, and the
   bike-vs-transit choice is a rain on/off switch (`decide()` in
   `app/services/advice.py`). It will recommend transit on a drizzle even if transit is
   3x slower, and it never compares door-to-door cost.

2. **No way to mix bike + transit + walk.** Today the backend fires two separate OTP
   queries — `mode="BICYCLE"` and `mode="TRANSIT,WALK"` — and presents one or the
   other. It never asks OTP for `BICYCLE,TRANSIT,WALK`, so a "bike to the metro, ride,
   walk to the door" trip is impossible to surface, even though OTP supports it natively.

## Goals

- Produce **mixed itineraries** (bike-and-ride: bike + transit + walk in one trip).
- Rank **all** candidate itineraries with a single **rain-aware generalized cost**, not a
  binary switch. The rain-awareness stays the product's identity, but becomes a real
  optimization (intensity- and exposure-weighted) instead of an on/off rule.
- Return a **ranked list of options** (typically 2-3) with one flagged recommended.
- Keep the existing **resilience guarantees**: bounded upstream calls, graceful
  degradation, never fabricate a route on failure.

## Non-goals (now)

- **OV-fiets / shared-bike (BICYCLE_RENT).** Not implemented now, but the schema is
  designed so it can be added later with no API/frontend contract change (see §8). It
  requires a GBFS feed in the OTP graph.
- **NS national rail.** The OTP graph is GVB-only (`gtfs-gvb.zip`); trains are out of
  scope. "Bike-and-ride" here means biking to a GVB metro/tram/bus stop.
- **Bike-parking modeling** (secure parking near stops). OTP's own bike-and-ride
  transitions are trusted as-is.
- **User-tunable weights at runtime.** Scoring weights are named module constants
  (tunable in code), not request parameters.

## Decisions (from brainstorming)

| Question | Decision |
| --- | --- |
| What does "mix" produce? | **Bike-and-ride now, OV-fiets later.** Add `BICYCLE,TRANSIT,WALK` candidates against the current graph; design for OV-fiets later. |
| What does "best" optimize? | **Rain-aware generalized cost**: door-to-door time + per-minute rain-exposure penalty on cycling/walking legs + small transfer penalty. |
| How to present results? | **Ranked list of options**, one flagged recommended. Frontend shows all (2-3), recommended highlighted + expanded, others compact. |

## Approach (chosen: A)

**A — Three parallel OTP queries + one unified ranking.** Issue `BICYCLE`,
`TRANSIT,WALK`, and `BICYCLE,TRANSIT,WALK` concurrently (`asyncio.gather`), pool the
itineraries, tag each by kind, dedup, score with the rain-aware cost, rank.

- Rejected **B** (single combined query): OTP optimizing a combined plan may not surface
  a clean pure-bike or pure-transit option, losing the bike baseline the rain logic needs
  and options the product wants to always show.
- Rejected **C** (hand-rolled hybrid enumeration via the stops DB): reinvents OTP's
  bike-and-ride and contradicts the "routing is delegated" principle.

Three calls instead of two, run in parallel, so latency is ~unchanged (bounded by the
existing per-call timeout).

---

## Architecture

### 1. Candidate generation — `app/services/planner.py` (new)

A thin orchestration service between the API handlers and `OTPClient`.

```
async def gather_candidates(otp, from_place, to_place, departure=None) -> list[Candidate]
```

- Run three `otp.plan(...)` calls concurrently with `asyncio.gather(return_exceptions=True)`:
  `BICYCLE`, `TRANSIT,WALK`, `BICYCLE,TRANSIT,WALK`.
- An `OTPError` from any single query drops only that query's candidates (the others still
  produce a result). If **all** fail, raise `OTPError` up to the handler (-> 502), matching
  today's behaviour.
- Flatten the pooled itineraries into `Candidate` objects, each tagged with `kind`:
  - `bike` — every leg is `BICYCLE` (walk-only access legs allowed if OTP emits them).
  - `transit` — has a transit leg, no `BICYCLE` leg.
  - `bike_and_ride` — has both a `BICYCLE` leg and a transit leg.
  - Pure WALK-only itineraries are dropped (same reasoning as today's
    `first_transit_itinerary`: walking leaves the rider just as wet).
- **Dedup**: collapse itineraries with the same leg-mode signature whose total duration is
  within 60s of each other; keep the shortest. Keep at most one per `kind` for the top
  result, but retain runners-up so the ranked list can show alternatives.

`Candidate` is an internal dataclass/pydantic model: `{ kind, itinerary: Itinerary }`
reusing the existing `app/clients/otp.py::Itinerary`.

### 2. Rain-aware cost model — `app/services/scoring.py` (new, pure)

A pure function (no I/O, fully unit-testable with fixtures), mirroring the discipline of
the current `decide()`.

```
def score(candidate: Candidate, rain: RainForecast | None,
          weights: Weights = DEFAULT_WEIGHTS) -> ScoredCandidate
```

Generalized cost (lower is better):

```
cost = total_minutes
     + sum over exposed legs (BICYCLE | WALK):
           exposed_minutes(leg) * lambda(peak_mm_h within leg's window)
     + transfer_count * tau
```

- **Exposed legs**: `BICYCLE` and `WALK`. Transit (TRAM/BUS/METRO/RAIL/FERRY) is sheltered
  -> no rain penalty.
- **Per-leg rain window**: convert each leg's `start_time`/`end_time` (epoch ms) to
  Europe/Amsterdam wall-clock (reuse `_local_time` from `advice.py`), intersect with the
  Buienradar slots, take wet slots (`>= DEFAULT_RAIN_THRESHOLD_MM_H = 0.1`).
  `exposed_minutes` = minutes of the leg overlapping wet slots (5-min slot granularity);
  `peak_mm_h` = max intensity within the leg window.
- **`lambda(intensity)`** — penalty multiplier on exposed minutes (starting constants,
  tunable):

  | peak mm/h | band | lambda |
  | --- | --- | --- |
  | < 0.1 | dry | 0 |
  | 0.1 – 0.5 | drizzle | 0.5 |
  | 0.5 – 1.5 | light | 1.5 |
  | 1.5 – 4.0 | moderate | 3.0 |
  | > 4.0 | heavy | 6.0 |

  Example: 10 min cycling in heavy rain adds 60 cost-minutes -> a 15-min ride costs ~75,
  losing to a 25-min sheltered transit option. A dry 15-min ride costs 15 and wins.

- **`tau` (transfer penalty)** = 4 cost-minutes per transfer (`transfer_count` = number of
  boardings minus 1, min 0). Keeps a 1-extra-transfer mixed trip from beating a simpler
  one on a tie.
- **`transfer_count`** counts transit boardings (legs whose mode is a transit mode); a
  bike-and-ride with one metro ride has 0 transfers, bike + metro + tram has 1.

`Weights` is a frozen dataclass holding `lambda` bands and `tau`, with a `DEFAULT_WEIGHTS`
module constant.

**No-forecast degradation**: `rain is None` -> all `lambda` contributions are 0, so cost =
time + transfers; the fastest sensible option wins and the reason flags the forecast as
unknown.

### 3. Ranking + reason — refactor `app/services/advice.py`

Replace the binary `decide()` with:

```
def rank(candidates: list[Candidate], rain: RainForecast | None) -> RankedPlan
```

- Score every candidate, sort ascending by cost, mark index 0 `recommended`.
- Generate the human `reason` for the recommended option, reusing the existing phrasing
  style and rain vocabulary. Examples:
  - dry bike: `"dry during your 18-min ride -> bike"`
  - rain pushes to mix: `"rain ~14:05 (~2.1 mm/h) -> bike to Spaklerweg, then metro 50 (22 min)"`
  - rain, transit best: `"rain ~14:05 (~2.1 mm/h) -> take tram 13 (24 min)"`
  - no forecast: `"rain forecast unavailable -> fastest is bike (18 min)"`
- `RankedPlan` carries the ordered scored candidates plus top-level rain summary
  (`max_rain_mm_per_h`, `rain_expected`) computed over the recommended option's exposed
  legs (so the existing top-level fields keep their meaning).

`decide()`'s existing unit tests are migrated to `rank()` with equivalent scenarios; the
old function is removed. It has **three** call sites, all updated:
`app/api/advice.py`, `app/api/plan.py`, and the rain-alert flow in
`app/services/notify.py` (driven by the ARQ worker in `app/workers/main.py`).

**`notify.py` must preserve its warn semantics.** Today it calls `decide()` and notifies
only when rain is expected for the cycling window and a drier alternative exists. After
the refactor it calls `rank(...)` and warns when the recommended option is **not pure
bike because of rain** — i.e. rain on the cycling window pushed a transit or
bike-and-ride option above pure bike. A dry ride (recommended `bike`) does not warn, and
no-forecast does not warn (unchanged). `notify.py` keeps planning at the alert's
scheduled `departure` time (it already threads `departure` into the OTP calls).

### 4. Backend API contract — `app/schemas/plan.py`, `app/api/plan.py`, `app/api/advice.py`

**`/v1/plan`** gains a ranked `options` list. New/changed schema:

```python
OptionKind = Literal["bike", "transit", "bike_and_ride"]

class OptionOut(BaseModel):
    kind: OptionKind
    recommended: bool
    score: float                 # generalized cost (debug/sort aid; client may ignore)
    rain_minutes: int            # exposed cycling/walking minutes in rain (0 if dry)
    itinerary: ItineraryOut

class PlanResponse(BaseModel):
    recommendation: OptionKind   # == the recommended option's kind
    reason: str
    max_rain_mm_per_h: float | None
    rain_expected: bool | None
    origin: PlaceOut
    destination: PlaceOut
    options: list[OptionOut]     # ranked, recommended first
```

- The previous `bike: ItineraryOut` / `transit: ItineraryOut | None` fields are
  **removed**; the frontend (ours) is updated in lockstep. `recommendation` widens to
  include `"bike_and_ride"`.
- `ItineraryOut`, `LegOut`, `StepOut`, `PlaceOut` are unchanged — mixed itineraries are
  already expressible as a list of per-mode legs.

**`/v1/advice`** stays a lightweight summary. `AdviceResponse.recommendation` widens to
`OptionKind`; it reports the recommended option's kind, reason, and minutes. (It can
report `bike_and_ride` now.) No `options` list on advice — that's the plan endpoint's job.

**`/v1/plan` and `/v1/advice` handlers** both call `planner.gather_candidates(...)` then
`advice.rank(...)`, replacing the two hand-coded OTP calls + `decide(...)`.

**Errors** unchanged: unknown place -> 400; geocoder upstream -> 502; all OTP queries fail
-> 502 "routing upstream unavailable"; no candidates at all -> 502.

### 5. Frontend — `fiets-of-ov-frontend`

- **`src/api/types.ts`**: `Mode` += `"bike_and_ride"`. Add `Option` (`kind`,
  `recommended`, `rain_minutes`, `itinerary: Itinerary`). `Plan` replaces `bike`/`transit`
  with `options: Option[]`; keep `recommendation`, `reason`, rain fields.
- **`src/lib/planView.ts`**: build view-models from the ranked `options` list instead of
  fixed bike/transit slots. Recommended option first/expanded.
- **`src/components/ResultsPanel.tsx`** (+ `AdviceCard`, `ItineraryDetails`): render N
  ranked options; recommended highlighted and expanded; others compact cards showing
  kind, total minutes, and exposed rain minutes. Selecting an option draws it.
- **`src/components/MapView.tsx`**: already draws one polyline per leg, styled by mode, so
  a mixed itinerary (bike legs + transit legs) renders without structural change. Verify
  the bike-leg style is correct when it appears inside a mixed itinerary; add a legend
  entry for bike-and-ride if needed.
- **`src/api/mock.ts`**: add a `bike_and_ride` itinerary and the `options` array so
  offline/dev mode and tests cover the mixed + ranked case. Update `mockPlanFor` /
  `mockAdviceFor`.

### 6. Testing

- **Backend — `tests/unit/test_scoring.py`** (new, pure): dry -> fastest wins; heavy rain
  -> long bike leg penalized, transit/bike-and-ride wins; partial rain -> bike-and-ride
  (short exposed bike leg) beats pure bike; no forecast -> fastest wins; transfer penalty
  breaks ties.
- **Backend — `test_planner.py`** (new): three-query fan-out, kind tagging, dedup,
  partial-failure degradation (one query errors -> others still ranked), all-fail -> raise.
- **Backend — `test_advice.py` / `test_advice_endpoint.py` / `test_plan` endpoint**:
  migrate `decide` tests to `rank`; endpoint tests mock three OTP calls via respx and
  assert the ranked `options` shape and recommended selection. Keep the existing
  degradation tests (Buienradar down, transit unavailable, OTP down).
- **Backend — `test_notify.py`**: warn when rain pushes the recommendation off pure bike;
  do not warn on a dry ride or when the forecast is unavailable (preserve current
  semantics against `rank()`).
- **Frontend**: `planView` ranking tests; `ResultsPanel` renders multiple options and
  highlights the recommended one; mock parity with the new shape.

### 7. Rollout / phasing

1. Backend: `scoring.py` + `planner.py` + `rank()` with unit tests (no API change yet).
2. Backend: switch `/v1/plan` + `/v1/advice` handlers **and `app/services/notify.py`** to
   the new pipeline; update schemas + endpoint + notify tests.
3. Frontend: types + planView + mock, then ResultsPanel/MapView; update tests.
4. Manual end-to-end against local OTP (`docker compose -f docker-compose.yml -f
   docker-compose.otp.yml up`) on a few Amsterdam trips, checking that a rainy forecast
   surfaces a bike-and-ride or transit option above pure bike.

### 8. OV-fiets future-proofing (no implementation now)

The contract is already generic enough that enabling OV-fiets requires no API/frontend
contract change:

- Add a GBFS feed to `otp/build-config.json` and rebuild the graph.
- Add `BICYCLE_RENT` to the mixed mode string in `planner.py` (e.g. a fourth query, or
  fold into the existing mixed query).
- Rented-bike legs arrive as ordinary `BICYCLE`/`BICYCLE_RENT` legs in `ItineraryOut`; the
  only client work is a new mode label/icon. `OptionKind` may gain a value later, but
  existing kinds keep working.

## Risks / mitigations

- **OTP latency from 3 calls**: parallelized; bounded by the existing per-call timeout.
  If it bites, drop to two calls (`BICYCLE` + `BICYCLE,TRANSIT,WALK`) and derive the
  transit candidate from the mixed query's transit-only itinerary.
- **Weight tuning is subjective**: weights are named constants with documented starting
  values and covered by scoring unit tests asserting *ordering* (not exact costs), so
  retuning won't churn tests.
- **Dedup hides a valid option**: dedup only collapses same-mode-signature itineraries
  within 60s; different kinds are never merged.
