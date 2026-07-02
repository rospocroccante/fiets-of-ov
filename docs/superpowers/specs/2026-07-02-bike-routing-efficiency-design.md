# Bike routing efficiency: direct routes, full coverage, honest times — design

Date: 2026-07-02
Status: approved (design)
Repo: `fiets-of-ov` (backend). No frontend changes.
Branch: continues `feat/amsterdam-pathfinding` (unmerged follow-up)
Supersedes: the bike-triangle and bike-speed values chosen in
`2026-07-01-amsterdam-pathfinding-design.md` (Layer 1). Everything else in that spec
(cost-based selection, local-knowledge weights) stands.

## Problem

User-reported, three concrete failures for bike routing:

1. **Indirect routes.** `optimize: TRIANGLE` with `{safetyFactor: 0.4, slopeFactor: 0.3,
   timeFactor: 0.3}` puts only 30% weight on time. Slope weight is wasted (Amsterdam is
   flat), and the safety weight makes OTP detour for "safe" streets in a city where nearly
   every street is already bike-safe. Routes come out longer than what a local would ride.
2. **Incomplete coverage.** The graph is built from the BBBike `Amsterdam.osm.pbf` extract,
   whose bounding box clips the city edges (parts of Zuidoost, outer Noord, Amstelveen,
   Diemen). Trips touching the clipped area fail or produce truncated routes.
3. **Wrong time estimates.** `bikeSpeed 4.3 m/s` (15.5 km/h) is slow for a Dutch utility
   cyclist (~17 km/h). Config is also internally inconsistent: `router-config.json` says
   `bicycle.speed: 4.5` while the client sends 4.3.

Direction chosen by the user: bike routes should be the **fastest/direct** route a local
would actually ride, not the maximally safe one.

## Goals

1. Bike (and bike-leg) routes are direct: time-dominant triangle.
2. The graph covers all of gemeente Amsterdam plus bordering municipalities.
3. Estimated bike minutes match reality for an average Dutch cyclist.
4. Before/after evidence via a golden-trip validation script.

## Non-goals (YAGNI)

- No second routing engine (GraphHopper etc.). Escalate only if OTP still produces bad
  routes after this tuning, with golden-trip evidence.
- No per-user route-style preference (fast vs relaxed) in UI or API.
- No GTFS coverage change: the transit feed stays filtered to GVB. The coverage failure is
  in the street (OSM) graph, not transit.
- No frontend or API schema changes.

## Design

### 1. Routing parameters (`app/clients/otp.py`)

| Constant | Old | New | Why |
| --- | --- | --- | --- |
| `_BIKE_TRIANGLE` | `{safety 0.4, slope 0.3, time 0.3}` | `{safetyFactor: 0.3, slopeFactor: 0.0, timeFactor: 0.7}` | time-dominant = direct routes; residual safety keeps fietspad preference over parallel car roads; slope irrelevant in a flat city. Sums to 1.0. |
| `_BIKE_SPEED` | 4.3 | 4.7 | ~17 km/h, average Dutch utility cyclist |

Everything else in the query (searchWindow, reluctances, transfer costs, the
optimize/triangle-only-for-BICYCLE-modes gating) is unchanged. Not pure `QUICK`: zero
safety weight risks routing down busy car roads where a parallel fietspad exists.

### 2. OSM coverage (`otp/`)

- Replace the BBBike extract with Geofabrik
  `https://download.geofabrik.de/europe/netherlands/noord-holland-latest.osm.pbf`
  (~130 MB). Covers the whole gemeente plus Amstelveen, Diemen, Ouder-Amstel, Zaandam.
- `otp/build-config.json`: `osm.source` -> `noord-holland-latest.osm.pbf`.
- `otp/README.md`: update the data-file table and download instructions.
- Rebuild: delete `otp/data/graph.obj`, re-run `otp/scripts/run_otp.sh` (build is one-time;
  default 8g heap is expected to suffice for a provincial extract — if the build OOMs,
  raise `OTP_HEAP`).

### 3. Config alignment (`otp/router-config.json`)

`routingDefaults` must not contradict what the client sends (client values win at request
time, but drift invites confusion): `bicycle.speed` 4.5 -> 4.7, `numItineraries` 3 -> 5.

### 4. Golden-trip validation (`otp/scripts/golden_trips.py`, new)

A standalone script (not a unit test — needs the live OTP at :8080) that runs ~8-10 fixed
bike trips spanning the city and its edges, e.g.:

- Centraal -> Bijlmer ArenA (cross-city south-east)
- Noord (NDSM) -> Nieuw-West (Osdorpplein) (ferry + cross-city)
- Centraal -> Amstelveen Stadshart (edge: previously clipped)
- Diemen Sniep -> Vondelpark (edge: previously clipped)
- Zuid -> Sloterdijk, IJburg -> Centraal, De Pijp -> Noorderpark, Science Park -> RAI

For each trip it prints distance (m), duration (min), and straight-line detour ratio
(route distance / haversine distance). Output is compared before/after the change and
against external references (Google Maps bike estimates) by hand; expected outcomes:

- detour ratio drops on trips where the old triangle detoured,
- edge trips (Amstelveen, Diemen) return routes instead of failing,
- durations land within ~10% of external references.

Run once on the old graph+params before implementing (baseline capture), once after.

## Error handling

Unchanged. OTP failures still surface as `OTPError`; out-of-graph coordinates still fail
explicitly — the larger extract removes the real-world occurrences, not the guard. The
golden-trip script exits non-zero if any trip errors, so regressions are loud.

## Testing

- `tests/unit/test_otp.py`: update the asserted values for `bikeSpeed` (4.7) and the
  triangle (`{0.3, 0.0, 0.7}`); the presence/absence gating tests stay as-is.
- Existing scoring/planner/advice tests: unaffected (no cost-model change). Any test that
  hard-codes 4.3 gets updated.
- Live validation via the golden-trip script (section 4), evidence captured in the verify
  step.

## File structure

- Create: `otp/scripts/golden_trips.py`.
- Modify: `app/clients/otp.py`, `otp/build-config.json`, `otp/router-config.json`,
  `otp/README.md`, `tests/unit/test_otp.py`.
- Rebuilt artifact (not committed): `otp/data/graph.obj` from
  `noord-holland-latest.osm.pbf`.

## Risks / notes

- Triangle and speed values remain judgment calls; the golden-trip script exists precisely
  to check them against reality, and they are single documented constants to adjust.
- The provincial extract grows the graph (build time and memory). One-time build cost;
  if 8g heap fails, raise `OTP_HEAP`.
- Faster, more direct bike routes lower bike costs relative to transit, so the bike option
  wins more often in recommendations. This is the intended effect; the existing
  `transit_bias_min` (10 min) still protects transit when it is clearly better.
