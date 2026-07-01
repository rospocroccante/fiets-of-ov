# Amsterdam Pathfinding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Better routing for Amsterdam — tuned OTP requests, cost-based per-kind selection,
and curated local-knowledge weights (hub-aware transfers, ferry-for-Noord, bike-to-station).

**Architecture:** Add an `amsterdam.py` local-knowledge module (pure), extend `scoring`
weights + cost, move per-kind selection from duration (planner) to generalized cost
(`rank`), and tune the OTP GraphQL request. Backend only; response schema unchanged.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, httpx, OpenTripPlanner OTP2 GTFS GraphQL,
pytest + respx + ruff. Tests run offline (respx mocks OTP; scoring/amsterdam are pure).

## Global Constraints

- No response-schema change: `options` stays one-per-kind `{kind, recommended, score,
  rain_minutes, itinerary}`, best-first.
- Local knowledge is hardcoded curated constants in code (no env/config, no external data).
- `rank()` is the single per-kind selection point (lowest cost per kind, sorted best-first).
- OTP parameters are documented in-code constants; only send `optimize`/`triangle` for mode
  strings containing `BICYCLE`.
- No emoji, no AI/Claude trailer in commits or files. TDD; commit once per task.
- Commands (run in repo root `/Users/Rospo/Vibecoding/fiets-of-ov`, venv active):
  `.venv/bin/pytest -q`, `.venv/bin/ruff check app tests`. Use `.venv/bin/python -m pytest`
  if `pytest` is not on PATH. Do NOT require a live OTP or DB (respx/pure only).

---

### Task 1: Amsterdam local-knowledge module (`app/services/amsterdam.py`)

**Files:**
- Create: `app/services/amsterdam.py`
- Test: `tests/unit/test_amsterdam.py`

**Consumes:** `app.clients.otp.Itinerary`, `app.clients.otp.Leg`,
`app.services.geo.haversine_m(lat1, lon1, lat2, lon2) -> float` (metres).

**Produces:**
```python
HUB_RADIUS_M: float = 250.0
MAJOR_HUBS: tuple[tuple[str, float, float], ...]   # (name, lat, lon), see spec list
def is_near_hub(lat: float | None, lon: float | None) -> bool
def transfer_points(itinerary: Itinerary) -> list[tuple[float, float]]
def has_ferry(itinerary: Itinerary) -> bool
def bike_handoff_point(itinerary: Itinerary) -> tuple[float, float] | None
```

**Behavior:**
- `_TRANSIT_MODES` = a leg is transit when `leg.mode not in {"WALK", "BICYCLE"}`.
- `is_near_hub`: False if lat or lon is None; else True when `haversine_m(lat, lon, h_lat,
  h_lon) <= HUB_RADIUS_M` for any hub.
- `transfer_points`: collect transit legs in order; for each transit leg after the first,
  append `(leg.from_lat, leg.from_lon)` when both are non-None. (One point per transfer.)
- `has_ferry`: any `leg.mode == "FERRY"`.
- `bike_handoff_point`: for the first `leg.mode == "BICYCLE"`, return `(leg.to_lat,
  leg.to_lon)` if both non-None, else None; None if no bike leg.

**Test cases (`test_amsterdam.py`):** build `Leg`/`Itinerary` like `tests/unit/test_planner.py`
(import its `leg`/`itin` shape or construct directly with lat/lon fields).
- `is_near_hub(52.3791, 4.9003)` True (Centraal); `is_near_hub(52.30, 4.75)` False;
  `is_near_hub(None, 4.9)` False.
- `has_ferry` True for an itinerary with a `FERRY` leg, False otherwise.
- `transfer_points`: 0 for a single transit leg; 1 for two transit legs; 2 for three; a
  WALK between transit legs does not add a point.
- `bike_handoff_point`: returns the bike leg's `(to_lat, to_lon)`; None when no bike leg.

- [ ] Step 1: Write `test_amsterdam.py` (failing).
- [ ] Step 2: Run it, verify failure.
- [ ] Step 3: Implement `amsterdam.py`.
- [ ] Step 4: `.venv/bin/pytest -q tests/unit/test_amsterdam.py` green; `ruff check` clean.
- [ ] Step 5: Commit `feat: Amsterdam local-knowledge module (hubs, ferry, bike handoff)`.

---

### Task 2: Expert weights in scoring (`app/services/scoring.py`)

**Files:**
- Modify: `app/services/scoring.py`
- Modify: `tests/unit/test_scoring.py`

**Consumes:** Task 1 (`is_near_hub`, `transfer_points`, `has_ferry`, `bike_handoff_point`).

**Changes:**
- `Weights` gains: `transfer_penalty_min: float = 5.0` (was 4.0),
  `hub_transfer_penalty_min: float = 2.0`, `ferry_bonus_min: float = 3.0`,
  `station_access_bonus_min: float = 2.0`. Keep `rain_bands`, `heavy_penalty`,
  `transit_bias_min = 10.0`.
- Replace the flat transfer cost with a per-transfer, hub-aware sum:
  ```python
  transfer_cost = sum(
      weights.hub_transfer_penalty_min if amsterdam.is_near_hub(lat, lon)
      else weights.transfer_penalty_min
      for (lat, lon) in amsterdam.transfer_points(itin)
  )
  ```
- Add `ferry_bonus = weights.ferry_bonus_min if amsterdam.has_ferry(itin) else 0.0`.
- Add station-access bonus:
  ```python
  station_bonus = (
      weights.station_access_bonus_min
      if candidate.kind == "bike_and_ride"
      and amsterdam.is_near_hub(*(amsterdam.bike_handoff_point(itin) or (None, None)))
      else 0.0
  )
  ```
- `cost = total_minutes + rain_cost + transfer_cost + bias - ferry_bonus - station_bonus`,
  still `round(cost, 4)`. `_transfer_count` may be removed if now unused.

**Test cases (update `test_scoring.py`):**
- Recompute any expected costs that used the old flat `transfers * 4`.
- Two transit itineraries differing only in transfer location: the one whose transfer point
  is near a hub scores lower.
- An itinerary with a `FERRY` leg scores `ferry_bonus_min` lower than the same without.
- `bike_and_ride` whose bike leg ends at a hub scores `station_access_bonus_min` lower than
  one ending away from any hub; a plain `bike`/`transit` gets no station bonus.
- Existing rain-band and transit-bias tests still pass (adjust numbers only where transfers
  are involved).

- [ ] Step 1: Update/add failing tests.
- [ ] Step 2: Run, verify failure.
- [ ] Step 3: Implement the weight + cost changes.
- [ ] Step 4: `.venv/bin/pytest -q tests/unit/test_scoring.py` green; `ruff check` clean.
- [ ] Step 5: Commit `feat: hub-aware transfers, ferry bonus, station-access bonus in cost`.

---

### Task 3: Cost-based per-kind selection (`scoring.rank` + `services/planner.py`)

**Files:**
- Modify: `app/services/scoring.py` (`rank`)
- Modify: `app/services/planner.py` (`gather_candidates`)
- Modify: `tests/unit/test_planner.py`, `tests/unit/test_scoring.py`

**Consumes:** Task 2.

**Changes:**
- `rank(candidates, rain, weights, threshold)`: score every candidate, then keep only the
  **lowest-cost candidate per kind** (group by `kind`, min by cost), and return that list
  sorted ascending by `(cost, itinerary.duration, kind)`. (Previously it scored and sorted
  whatever it was given, assuming one-per-kind.) Update the docstring to state this.
- `gather_candidates`: stop the best-per-kind-by-duration dedup. Classify every itinerary
  from all plans (drop `classify_kind is None`, i.e. walk-only) and return **all** as
  `Candidate`s. Snap fallback condition becomes `if not any(c.kind == "bike" for c in
  candidates) and candidates:` then append the snapped bike candidate.

**Test cases:**
- `test_scoring.py`: add — `rank()` given two `bike` candidates and one `transit` returns
  exactly one `bike` (the lower cost) and one `transit`, best-first.
- `test_planner.py`: rewrite `test_keeps_shortest_per_kind_across_queries` -> assert
  `gather_candidates` returns BOTH transit candidates now (selection moved to `rank`); keep
  `test_gathers_one_best_candidate_per_kind` (the fixtures yield one per kind, so the kind
  set is unchanged), and keep walk-only-drop, partial-failure, all-fail, and both snap tests
  green.

- [ ] Step 1: Update/add failing tests.
- [ ] Step 2: Run, verify failure.
- [ ] Step 3: Implement `rank` collapse + planner returns-all.
- [ ] Step 4: `.venv/bin/pytest -q tests/unit/test_planner.py tests/unit/test_scoring.py
  tests/unit/test_advice.py` green; `ruff check` clean.
- [ ] Step 5: Commit `feat: choose best itinerary per kind by generalized cost`.

---

### Task 4: OTP request tuning (`app/clients/otp.py`)

**Files:**
- Modify: `app/clients/otp.py`
- Modify: `tests/unit/test_otp.py`

**Changes:**
- Add a documented constant block near the top:
  ```python
  # Amsterdam-tuned OTP routing parameters (see design doc). All verified present on the
  # running OTP2 GTFS schema.
  _NUM_ITINERARIES = 5
  _SEARCH_WINDOW_S = 5400
  _WALK_RELUCTANCE = 2.5
  _WALK_SPEED = 1.35
  _BIKE_SPEED = 4.3
  _BIKE_RELUCTANCE = 1.7
  _TRANSFER_PENALTY = 180
  _WALK_BOARD_COST = 600
  _BIKE_TRIANGLE = {"safety": 0.4, "slope": 0.3, "time": 0.3}
  ```
- Extend `_PLAN_QUERY` with the new variables and pass them to `plan(...)`:
  `$searchWindow: Long`, `$walkReluctance: Float`, `$walkSpeed: Float`, `$bikeSpeed: Float`,
  `$bikeReluctance: Float`, `$transferPenalty: Int`, `$walkBoardCost: Int`,
  `$optimize: OptimizeType`, `$triangle: InputTriangle`. Wire each into the `plan(...)` call
  arguments (`numItineraries: $num` stays; set `$num` to `_NUM_ITINERARIES`).
- In `OTPClient.plan`, build `variables` with the tuned constants. Only include
  `optimize: "TRIANGLE"` and `triangle: _BIKE_TRIANGLE` when `"BICYCLE" in mode`; otherwise
  send `optimize: None` and `triangle: None` (so transit-only queries are unaffected).
- Keep `from`/`to`/`modes`/`date`/`time` exactly as today.

**Test cases (update `test_otp.py`):**
- Existing tests unchanged and still green.
- Add `test_plan_sends_tuned_routing_params`: after a `BICYCLE` plan, assert
  `variables["num"] == 5`, `variables["searchWindow"] == 5400`,
  `variables["walkReluctance"] == 2.5`, `variables["bikeSpeed"] == 4.3`,
  `variables["transferPenalty"] == 180`.
- Add `test_bike_triangle_only_for_bike_modes`: `BICYCLE` and `BICYCLE,TRANSIT,WALK` send
  `variables["optimize"] == "TRIANGLE"` and `variables["triangle"] ==
  {"safety":0.4,"slope":0.3,"time":0.3}`; `TRANSIT,WALK` sends `variables["optimize"] is
  None` and `variables["triangle"] is None`.

- [ ] Step 1: Add failing tests.
- [ ] Step 2: Run, verify failure.
- [ ] Step 3: Implement the query + variable changes.
- [ ] Step 4: `.venv/bin/pytest -q tests/unit/test_otp.py` green, then full
  `.venv/bin/pytest -q` green; `ruff check app tests` clean.
- [ ] Step 5: Commit `feat: Amsterdam-tuned OTP routing parameters + safe bike routing`.

---

## Self-review checklist (author)

- Spec coverage: OTP tuning (T4), cost-based per-kind selection (T3), hub/ferry/station
  weights (T2) backed by the local-knowledge module (T1). Covered.
- Type consistency: `Weights` fields, `OptionKind`, `Itinerary`/`Leg` fields (`from_lat`,
  `to_lat`, ...), `rank()` signature unchanged; `amsterdam` helpers typed as above.
- No placeholders: every task lists interfaces, constants, and concrete test cases.
- Downstream contract (advice/api) unchanged — no edits required there beyond adjusting any
  hard-coded expected `score` numbers in their tests.
