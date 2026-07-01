# Better pathfinding + Amsterdam local knowledge — design

Date: 2026-07-01
Status: approved (design) — proceeding to plan + subagent execution
Repo: `fiets-of-ov` (backend)
Branch: `feat/amsterdam-pathfinding` (based on `main`)

## Problem

Routing quality is mediocre and generic. The OTP query sends only `numItineraries: 3` with
all other parameters at OTP defaults, and `gather_candidates` keeps the single **shortest**
itinerary per kind — so the rain/expert weights only reorder three fixed options, they never
pick a *better* path within a kind. There is no Amsterdam-specific knowledge (ferries to
Noord, comfortable transfers at major interchanges, the bike-to-station pattern).

## Goals (approved)

1. **Tune OTP** for Amsterdam realism so the raw itineraries are better (more candidates, a
   transit search window, realistic walk/bike speeds and reluctances, safe bike routing).
2. **Select per kind by generalized cost, not duration** — return all candidates from the
   planner and let scoring pick the lowest-cost itinerary per kind, so the weights actually
   choose the path.
3. **Encode curated Amsterdam local knowledge as weights** (in code, documented, tested):
   comfortable transfers at major hubs, a GVB-ferry bonus for Noord crossings, and a
   station-access bonus for the bike-to-station (bike-and-ride) pattern. Keep the existing
   bike-first bias and rain penalties.

Chosen heuristics: hub-aware transfers, ferry-for-Noord bonus, bike-first + station access.
Explicitly NOT included: metro/train-over-bus mode preference (deferred).

## Non-goals (YAGNI)

- No external data feeds; local knowledge is hardcoded curated constants.
- No metro-vs-bus mode weighting (`modeWeight`) for now.
- No frontend or schema change — `options` stays one-per-kind with `{kind, recommended,
  score, rain_minutes, itinerary}`; only *how* the per-kind path is chosen changes, and the
  meaning of `score` (now includes the new weights).
- No config/env surface for the new constants (in-code, per the approved approach).

## Architecture

### Layer 1 — OTP request tuning (`app/clients/otp.py`)

All parameters below are verified present on the running OTP2 GTFS schema. Add them as a
documented in-code constant block (`_ROUTING`) and extend `_PLAN_QUERY` + `variables`:

| Param | Value | Why |
| --- | --- | --- |
| `numItineraries` | 5 | more candidates to choose the best-per-kind from (was 3) |
| `searchWindow` | 5400 | 90-min departure window → better transit options |
| `walkReluctance` | 2.5 | Amsterdammers avoid long walks (bias toward bike/transit) |
| `walkSpeed` | 1.35 | realistic pedestrian speed (m/s) |
| `bikeSpeed` | 4.3 | ~15.5 km/h realistic urban cycling (m/s) |
| `bikeReluctance` | 1.7 | slightly favour cycling over walking in mixed trips |
| `transferPenalty` | 180 | discourage frivolous transfers (OTP cost seconds) |
| `walkBoardCost` | 600 | OTP default; keep explicit for clarity |

Bike-only routing quality: for mode strings containing `BICYCLE`, also send
`optimize: TRIANGLE` and `triangle: {safety: 0.4, slope: 0.3, time: 0.3}` (sums to 1.0) so
bike legs prefer safe, comfortable routes. Do NOT send `optimize`/`triangle` for
transit-only (`TRANSIT,WALK`) queries. `date`/`time`/`from`/`to`/`modes` behaviour is
unchanged.

### Layer 2 — cost-based per-kind selection (`services/planner.py` + `scoring.rank`)

`gather_candidates` stops collapsing to one-per-kind by duration: it classifies every
itinerary from all three queries and returns **all** of them (walk-only still dropped; snap
fallback unchanged, guarded by "no bike candidate present"). The per-kind winner is chosen
by cost inside `rank()`.

`rank(candidates, rain, weights)` becomes the single selection point: score every candidate,
keep the **lowest-cost candidate per kind**, and return that set sorted ascending by cost
(ties: shorter duration, then kind name). Downstream contract is unchanged — `advice.recommend`
and `api/plan` still receive one-per-kind, best-first — so they need no change.

### Layer 3 — Amsterdam local knowledge (`services/amsterdam.py`, new) + `scoring.Weights`

New pure module `app/services/amsterdam.py` (no I/O), reusing `services/geo.haversine_m`:

```python
HUB_RADIUS_M = 250.0
MAJOR_HUBS: tuple[tuple[str, float, float], ...] = (
    ("Amsterdam Centraal", 52.3791, 4.9003),
    ("Amsterdam Zuid",      52.3389, 4.8730),
    ("Amstel",              52.3467, 4.9177),
    ("Sloterdijk",          52.3889, 4.8375),
    ("Lelylaan",            52.3576, 4.8378),
    ("Bijlmer ArenA",       52.3122, 4.9471),
    ("Duivendrecht",        52.3268, 4.9370),
    ("RAI",                 52.3376, 4.8896),
    ("Muiderpoort",         52.3603, 4.9280),
)

def is_near_hub(lat: float | None, lon: float | None) -> bool: ...   # within HUB_RADIUS_M of any hub
def transfer_points(itinerary) -> list[tuple[float, float]]: ...     # (lat,lon) at each transfer
def has_ferry(itinerary) -> bool: ...                                # any leg.mode == "FERRY"
def bike_handoff_point(itinerary) -> tuple[float, float] | None: ... # bike leg's to-coords
```

- `transfer_points`: transit legs are legs whose mode is not in `{WALK, BICYCLE}`. A transfer
  occurs between consecutive transit boardings; the transfer point is the `from` (lat,lon) of
  each transit leg after the first transit leg. Returns one point per transfer.
- `bike_handoff_point`: the `to` (lat,lon) of the (first) BICYCLE leg, or None.

`scoring.Weights` gains:

```python
transfer_penalty_min: float = 5.0        # non-hub transfer (was 4.0)
hub_transfer_penalty_min: float = 2.0    # transfer at a MAJOR_HUB is smoother
ferry_bonus_min: float = 3.0             # subtracted when a FERRY leg is used
station_access_bonus_min: float = 2.0    # bike-and-ride whose bike ends at a hub
# unchanged: rain_bands, heavy_penalty, transit_bias_min = 10.0
```

`scoring.score()` changes:
- Transfer cost: per transfer point, `hub_transfer_penalty_min` if `is_near_hub(point)` else
  `transfer_penalty_min` (replaces the flat `_transfer_count * transfer_penalty_min`).
- Ferry: `-ferry_bonus_min` when `has_ferry(itin)`.
- Station access: for `bike_and_ride`, `-station_access_bonus_min` when
  `is_near_hub(bike_handoff_point(itin))`.
- Unchanged: `total_minutes`, rain cost, `transit_bias_min` (bike stays first unless transit
  saves >10 min). Cost is still rounded to 4 dp; bonuses may make a cost slightly negative in
  edge cases — that is fine (relative order is what matters), no clamping.

## Error handling

- OTP still raises `OTPError` on any failure; extra parameters do not change the failure
  contract. Malformed responses still surface as `OTPError`.
- `amsterdam.py` handles `None` coordinates defensively (`is_near_hub(None, ...) -> False`).
- Snap fallback unchanged; if no bike candidate exists after fan-out, it still fires.

## Testing

- `tests/unit/test_otp.py`: existing pass unchanged (they assert only from/to/modes/date/time).
  Add: tuned params (`numItineraries==5`, `searchWindow==5400`, `walkReluctance`, `bikeSpeed`,
  ...) are sent; `optimize/triangle` present for `BICYCLE` and `BICYCLE,TRANSIT,WALK` modes and
  absent for `TRANSIT,WALK`.
- `tests/unit/test_amsterdam.py` (new): `is_near_hub` true within 250 m of Centraal, false far
  away and for `None`; `has_ferry`; `transfer_points` count for 0/1/2-transfer itineraries;
  `bike_handoff_point`.
- `tests/unit/test_scoring.py`: update expected costs for the new transfer model; add tests —
  hub transfer cheaper than non-hub; ferry bonus lowers cost; station-access bonus applies
  only to bike-and-ride ending at a hub; `rank()` keeps the lowest-cost candidate per kind
  when several of the same kind are passed, sorted best-first.
- `tests/unit/test_planner.py`: `gather_candidates` now returns ALL classified candidates
  (rewrite `test_keeps_shortest_per_kind_across_queries` to assert both transit candidates are
  returned; move the "keep best per kind" assertion to a scoring/rank test). Walk-only drop,
  partial-failure, all-fail, and snap-fallback tests stay green.
- `tests/unit/test_advice.py`, `test_plan_endpoint.py`, `test_advice_endpoint.py`: stay green
  (contract unchanged); adjust any hard-coded expected `score`/cost numbers.

## Validation (live)

Unit tests cover the pure logic and the OTP variables offline. Real routing quality is
validated against the **running OTP (:8080)** after implementation, comparing before/after on
representative trips: a Noord crossing (expect ferry use), a cross-city trip transferring at a
major hub, and a short trip (expect bike-first). Captured as evidence in the verify step.

## File structure

- Create: `app/services/amsterdam.py`, `tests/unit/test_amsterdam.py`.
- Modify: `app/clients/otp.py`, `app/services/planner.py`, `app/services/scoring.py`, and the
  tests listed above.

## Risks / notes

- Tuning values are judgment calls; they are documented constants and easy to adjust after
  live comparison. The biggest quality wins are `numItineraries`, `searchWindow`, and
  cost-based per-kind selection.
- Cost can go slightly negative with bonuses; ordering is unaffected and no consumer treats
  `score` as non-negative (the frontend shows it as an opaque number).
