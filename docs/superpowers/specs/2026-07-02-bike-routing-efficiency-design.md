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
2. **Absurd IJ crossings.** ~~Incomplete coverage~~ — DISPROVEN by the golden-trip baseline
   (2026-07-02): all eight trips including Amstelveen and Diemen route fine on the BBBike
   extract (its bbox is (4.56,52.03)-(5.21,52.51), far larger than assumed). The real
   route-quality failure the baseline exposed: pure-bike trips crossing the IJ detour 2.6x
   (NDSM -> Osdorpplein: 21.8 km for an 8.4 km crow-fly) because mode `BICYCLE` excludes
   ferries (FERRY is a transit mode), so OTP rides around the water. Real Amsterdam
   cyclists roll their bike onto the free GVB ferry.
3. **Wrong time estimates.** `bikeSpeed 4.3 m/s` (15.5 km/h) is slow for a Dutch utility
   cyclist (~17 km/h). Config is also internally inconsistent: `router-config.json` says
   `bicycle.speed: 4.5` while the client sends 4.3.

Direction chosen by the user: bike routes should be the **fastest/direct** route a local
would actually ride, not the maximally safe one.

## Goals

1. Bike (and bike-leg) routes are direct: time-dominant triangle.
2. Pure-bike trips may use GVB ferries for IJ crossings (bike+ferry stays kind "bike").
3. Estimated bike minutes match reality for an average Dutch cyclist.
4. Before/after evidence via a golden-trip validation script.

## Non-goals (YAGNI)

- No OSM extract swap: the baseline proved the BBBike extract already covers Amstelveen,
  Diemen, and beyond. The golden edge trips stay as coverage regression checks.
- No second routing engine (GraphHopper etc.). Escalate only if OTP still produces bad
  routes after this tuning, with golden-trip evidence.
- No per-user route-style preference (fast vs relaxed) in UI or API.
- No GTFS coverage change: the transit feed stays filtered to GVB.
- No frontend or API schema changes (`kind` values are unchanged; a bike+ferry itinerary
  reports kind "bike" and its FERRY leg renders like any transit leg).

## Design

### 1. Routing parameters (`app/clients/otp.py`)

| Constant | Old | New | Why |
| --- | --- | --- | --- |
| `_BIKE_TRIANGLE` | `{safety 0.4, slope 0.3, time 0.3}` | `{safetyFactor: 0.3, slopeFactor: 0.0, timeFactor: 0.7}` | time-dominant = direct routes; residual safety keeps fietspad preference over parallel car roads; slope irrelevant in a flat city. Sums to 1.0. |
| `_BIKE_SPEED` | 4.3 | 4.7 | ~17 km/h, average Dutch utility cyclist |

Everything else in the query (searchWindow, reluctances, transfer costs, the
optimize/triangle-only-for-BICYCLE-modes gating) is unchanged. Not pure `QUICK`: zero
safety weight risks routing down busy car roads where a parallel fietspad exists.

### 2. Bike+ferry for IJ crossings (`app/services/planner.py`, `scoring.py`, `snap.py`)

- New shared constant `BIKE_MODES = "BICYCLE,FERRY"` in `planner.py`; used by
  `_MODE_SETS` (replacing `"BICYCLE"`), by `snap.py`'s probe query, and imported by the
  golden-trip script so the benchmark keeps sending exactly what the app sends.
- `scoring.classify_kind`: an itinerary with bike legs whose only transit mode is FERRY
  stays kind `"bike"` (a cyclist rolls the bike onto the ferry — still a bike trip).
  Bike + any non-ferry transit remains `"bike_and_ride"`; ferry/walk without bike remains
  `"transit"`; walk-only remains dropped.
- Rain exposure is already correct (FERRY not in `_EXPOSED_MODES`); the existing
  `ferry_bonus_min` now also rewards bike+ferry candidates — intended.
- Live risk, verified in validation: OTP only puts bikes on transit legs whose GTFS trips
  allow bikes. If the GVB feed lacks `bikes_allowed` on ferry trips, no ferry leg will
  appear; the contingency (NOT in this spec's scope — escalate first) is patching
  `otp/scripts/filter_gtfs_gvb.py` to set it and rebuilding the graph.

### 3. Router config (`otp/router-config.json`)

- Alignment: `routingDefaults` must not contradict what the client sends (client values
  win at request time, but drift invites confusion): `bicycle.speed` 4.5 -> 4.7,
  `numItineraries` 3 -> 5.
- `accessEgress: { maxDuration: "45m", maxStopCount: 5000 }` — REQUIRED for bike+ferry to
  work in practice. Discovered live: OTP 2.6's default `maxStopCount` (500) aborts the
  bike access/egress street search after touching 500 stops, which in Amsterdam's dense
  center happens ~11-12 bike-minutes out — before reaching the IJ ferry docks — so Raptor
  never saw a ferry option for any trip whose endpoint sits more than ~12 bike-minutes
  from a dock. Raising the cap surfaces bike+ferry itineraries (verified: NDSM ->
  Osdorpplein 99.9 -> 48.6 min). Deploy note: router-config changes need an OTP server
  restart (no graph rebuild).
- Benchmark selection: OTP can order the slower direct ride ahead of a bike+ferry
  itinerary, so `golden_trips.py` picks the fastest returned itinerary, mirroring the
  app's cheapest-per-kind selection in `scoring.rank` rather than trusting OTP's order.
- `intersectionTraversalModel: "CONSTANT"` — the default SIMPLE model adds turn-angle
  traversal time at every intersection, which in Amsterdam's dense grid inflated bike
  durations by a near-constant ~1.5 min/km (measured: Zuid -> Centraal 6.15 km took
  31.5 min, an effective 11.7 km/h, versus ~16 km/h on Google Maps and in reality).
  With CONSTANT the same leg takes 23.7 min (15.5 km/h effective) at the same
  `bikeSpeed 4.7` — matching a Dutch cyclist door-to-door. Evidence:
  `2026-07-02-golden-trips-after-constant-intersections.txt` (all eight trips at
  15-16 km/h effective; Centraal -> Bijlmer 43.2 -> 35.9 min). Trade-off: SIMPLE's
  per-turn costs marginally favour fewer-turn routes; the bike triangle's safety
  weight still steers route choice, so the loss is acceptable for honest durations.
  Remaining gap vs Google on ferry trips is the real GVB schedule wait plus actual
  crossing time, which Google does not count.

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

- the two IJ-crossing trips (NDSM -> Osdorpplein, De Pijp -> Noorderpark) drop from
  detour ~2.6 to roughly direct via a FERRY leg,
- detour ratio does not get dramatically worse on any other trip,
- edge trips (Amstelveen, Diemen) keep returning routes (coverage regression check),
- durations land within ~10% of external references.

Baseline captured 2026-07-02 on the old graph+params
(`docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt`); after-run compared
against it.

## Error handling

Unchanged. OTP failures still surface as `OTPError`; out-of-graph coordinates still fail
explicitly — the larger extract removes the real-world occurrences, not the guard. The
golden-trip script exits non-zero if any trip errors, so regressions are loud.

## Testing

- `tests/unit/test_otp.py`: update the asserted values for `bikeSpeed` (4.7) and the
  triangle (`{0.3, 0.0, 0.7}`); the presence/absence gating tests stay as-is.
- `tests/unit/test_scoring.py`: classify_kind cases — bike+ferry -> "bike", bike+tram ->
  "bike_and_ride" (unchanged), ferry+walk -> "transit" (unchanged).
- `tests/unit/test_planner.py` / `test_snap.py`: assert the bike queries send
  `BIKE_MODES` ("BICYCLE,FERRY").
- Live validation via the golden-trip script (section 4), evidence captured in the verify
  step.

## File structure

- Create: `otp/scripts/golden_trips.py`.
- Modify: `app/clients/otp.py`, `app/services/planner.py`, `app/services/scoring.py`,
  `app/services/snap.py`, `otp/router-config.json`, and the tests listed above.

## Risks / notes

- Triangle and speed values remain judgment calls; the golden-trip script exists precisely
  to check them against reality, and they are single documented constants to adjust.
- Bike-on-ferry depends on the GTFS `bikes_allowed` flag (see section 2 live risk).
- Faster, more direct bike routes lower bike costs relative to transit, so the bike option
  wins more often in recommendations. This is the intended effect; the existing
  `transit_bias_min` (10 min) still protects transit when it is clearly better.

## Addendum (2026-07-02, second tuning round): closing the gap with Google Maps

User comparison (Zuid -> NDSM): Google 30 min vs our 38-44. Root causes found and fixed:

1. `intersectionTraversalModel` SIMPLE (see section 3) — fixed earlier the same day.
2. `_BIKE_SPEED` 4.7 -> 5.0 (18 km/h cruise, ~16.3 km/h effective door-to-door with
   CONSTANT intersections — matches Google's bike estimates within a minute on all
   golden trips; evidence: `2026-07-02-golden-trips-final.txt`).
3. `_NUM_ITINERARIES` 5 -> 8: the fastest ferry variant (F7 Pontsteiger, Google's route)
   was often itinerary #6+ and never reached the ranker.
4. `scoring.rank` now counts DEPARTURE DELAY as cost (`Weights.depart_delay_factor`,
   1.0 min/min against the earliest candidate's start). Without it, raising the
   itinerary count made the ranker prefer a 33-min ferry route leaving in 50 minutes
   over a 38-min one leaving now. With it, the recommendation is the best door-to-door
   answer from "now".

Honest remaining gap vs Google on IJ trips: Google assumes the ideal ferry with zero
wait and an optimistic crossing (F7 headway is ~30 min midday; F4 crossing is a real
14 min). Our answer fluctuates 33-39 min with the actual timetable; when a ferry
aligns, we match Google's number with a route a local would actually catch.
