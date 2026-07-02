# Bike Routing Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Direct (time-dominant) bike routes, full-Amsterdam graph coverage, and realistic bike time estimates, with before/after golden-trip evidence.

**Architecture:** All changes in the backend repo `/Users/Rospo/Vibecoding/fiets-of-ov`. Routing itself stays in OTP2; we change the client-sent bike parameters (`app/clients/otp.py`), let pure-bike trips use GVB ferries for IJ crossings (`planner.py`/`scoring.py`/`snap.py`), and benchmark with a standalone script against the live OTP GraphQL API. (The originally planned OSM extract swap was dropped: the 2026-07-02 baseline proved the existing extract already covers the edge municipalities.)

**Tech Stack:** Python 3.12, httpx, pytest + respx (offline GraphQL mocks), uv for env/commands, OTP 2.6.0 (Java 21), Geofabrik OSM extracts.

**Spec:** `docs/superpowers/specs/2026-07-02-bike-routing-efficiency-design.md`

## Global Constraints

- Working directory for every command: `/Users/Rospo/Vibecoding/fiets-of-ov` (backend repo). NOT the frontend repo.
- Branch: `feat/amsterdam-pathfinding` (continue it; do not branch off).
- Run tests with: `uv run pytest tests/unit -q`.
- No emoji anywhere (code, docs, commits). No AI/Claude trailers in commit messages.
- New bike constants (exact values): `_BIKE_SPEED = 4.7`, `_BIKE_TRIANGLE = {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}`, `BIKE_MODES = "BICYCLE,FERRY"`.
- Task order matters: the baseline capture (Task 2) MUST run before the parameter change (Task 3), because the benchmark script imports the live constants from `app/clients/otp.py`. (Task 2 is complete: baseline committed at `docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt`.)

---

### Task 1: Golden-trips benchmark script

**Files:**
- Create: `otp/scripts/golden_trips.py`

**Interfaces:**
- Consumes: `_PLAN_QUERY` and the `_BIKE_*`/`_WALK_*`/`_NUM_ITINERARIES`/`_SEARCH_WINDOW_S`/`_TRANSFER_PENALTY`/`_WALK_BOARD_COST` constants from `app/clients/otp.py`; `haversine_m(lat1, lon1, lat2, lon2) -> float` from `app/services/geo.py`.
- Produces: CLI script `python otp/scripts/golden_trips.py [--otp URL]` printing one line per trip (km, min, detour ratio); exit 0 when all trips return a bike itinerary, exit 1 otherwise. Tasks 2 and 5 run it verbatim.

- [ ] **Step 1: Write the script**

Create `otp/scripts/golden_trips.py` with exactly this content:

```python
#!/usr/bin/env python3
"""Golden bike trips: routing-quality benchmark against a live OTP.

Runs a fixed set of bike-only trips spanning Amsterdam and its edges through the
OTP GraphQL API, printing distance, duration, and detour ratio (route distance /
straight-line distance) per trip. Run it before and after a graph or parameter
change and compare the outputs.

Usage:
    python otp/scripts/golden_trips.py [--otp http://localhost:8080]

Exit code 0 when every trip returned at least one bike itinerary, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable: the benchmark must send exactly the query and
# parameters the app sends, so it reuses the client's constants instead of
# duplicating values that would silently drift.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from app.clients.otp import (
    _BIKE_RELUCTANCE,
    _BIKE_SPEED,
    _BIKE_TRIANGLE,
    _NUM_ITINERARIES,
    _PLAN_QUERY,
    _SEARCH_WINDOW_S,
    _TRANSFER_PENALTY,
    _WALK_BOARD_COST,
    _WALK_RELUCTANCE,
    _WALK_SPEED,
)
from app.services.geo import haversine_m

# (name, from_lat, from_lon, to_lat, to_lon) — spans the city plus the edge areas
# that the old BBBike extract clipped (Amstelveen, Diemen).
TRIPS: tuple[tuple[str, float, float, float, float], ...] = (
    ("Centraal -> Bijlmer ArenA", 52.3791, 4.9003, 52.3122, 4.9471),
    ("NDSM -> Osdorpplein", 52.4010, 4.8935, 52.3585, 4.7900),
    ("Centraal -> Amstelveen Stadshart", 52.3791, 4.9003, 52.3010, 4.8630),
    ("Diemen Sniep -> Vondelpark", 52.3390, 4.9700, 52.3580, 4.8686),
    ("Zuid -> Sloterdijk", 52.3389, 4.8730, 52.3889, 4.8375),
    ("IJburg -> Centraal", 52.3560, 5.0010, 52.3791, 4.9003),
    ("De Pijp -> Noorderpark", 52.3556, 4.8926, 52.3920, 4.9190),
    ("Science Park -> RAI", 52.3540, 4.9540, 52.3376, 4.8896),
)


def plan_bike(
    client: httpx.Client,
    otp: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
) -> dict:
    """Return the first bike itinerary, raising RuntimeError when OTP has none."""
    variables = {
        "from": {"lat": from_lat, "lon": from_lon},
        "to": {"lat": to_lat, "lon": to_lon},
        "modes": [{"mode": "BICYCLE"}],
        "num": _NUM_ITINERARIES,
        "date": None,
        "time": None,
        "searchWindow": _SEARCH_WINDOW_S,
        "walkReluctance": _WALK_RELUCTANCE,
        "walkSpeed": _WALK_SPEED,
        "bikeSpeed": _BIKE_SPEED,
        "bikeReluctance": _BIKE_RELUCTANCE,
        "transferPenalty": _TRANSFER_PENALTY,
        "walkBoardCost": _WALK_BOARD_COST,
        "optimize": "TRIANGLE",
        "triangle": _BIKE_TRIANGLE,
    }
    response = client.post(
        f"{otp}/otp/gtfs/v1", json={"query": _PLAN_QUERY, "variables": variables}
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    itineraries = ((data.get("data") or {}).get("plan") or {}).get("itineraries") or []
    if not itineraries:
        raise RuntimeError("no itineraries")
    return itineraries[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--otp", default="http://localhost:8080", help="OTP base URL")
    args = parser.parse_args()
    otp = args.otp.rstrip("/")

    print(f"bikeSpeed={_BIKE_SPEED} triangle={_BIKE_TRIANGLE}")
    print(f"{'trip':<34} {'km':>7} {'min':>6} {'detour':>7}")
    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for name, from_lat, from_lon, to_lat, to_lon in TRIPS:
            try:
                itin = plan_bike(client, otp, from_lat, from_lon, to_lat, to_lon)
            except (httpx.HTTPError, RuntimeError) as exc:
                failures += 1
                print(f"{name:<34} FAILED: {exc}")
                continue
            distance_m = sum(leg.get("distance") or 0.0 for leg in itin["legs"])
            minutes = itin["duration"] / 60
            crow_m = haversine_m(from_lat, from_lon, to_lat, to_lon)
            detour = distance_m / crow_m if crow_m else float("nan")
            print(f"{name:<34} {distance_m / 1000:>7.2f} {minutes:>6.1f} {detour:>7.2f}")
    if failures:
        print(f"{failures}/{len(TRIPS)} trips failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify it parses and the imports resolve (no OTP needed)**

Run: `uv run python otp/scripts/golden_trips.py --help`
Expected: usage text printed, exit 0. (`--help` exits before any HTTP call.)

- [ ] **Step 3: Verify lint passes**

Run: `uv run ruff check otp/scripts/golden_trips.py`
Expected: no errors (ruff's default rules do not flag importing private names). Fix any genuine findings such as unused imports; do not add noqa comments.

- [ ] **Step 4: Commit**

```bash
git add otp/scripts/golden_trips.py
git commit -m "feat: golden-trip bike routing benchmark script"
```

---

### Task 2: Baseline capture (old graph, old parameters)

**Files:**
- Create: `docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt`

**Interfaces:**
- Consumes: `otp/scripts/golden_trips.py` (Task 1), a live OTP on `:8080` serving the OLD graph (`amsterdam.osm.pbf`) with the OLD client constants still in place.
- Produces: committed baseline output that Task 5 compares against.

**IMPORTANT:** This task must run BEFORE Task 3 edits the constants. The script imports `_BIKE_SPEED`/`_BIKE_TRIANGLE` live, so running it now measures the old configuration.

- [ ] **Step 1: Ensure OTP is running with the old graph**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/otp/gtfs/v1 -X POST -H 'Content-Type: application/json' -d '{"query":"{__typename}"}'`
Expected: `200`.

If not running, start it in the background (Java 21 required):
```bash
nohup otp/scripts/run_otp.sh > /tmp/otp-old.log 2>&1 &
```
then poll the curl above until `200` (graph already exists, so startup is load-only, ~1-2 min).

- [ ] **Step 2: Capture the baseline**

```bash
mkdir -p docs/superpowers/evidence
uv run python otp/scripts/golden_trips.py | tee docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt
```
Expected: header line shows `bikeSpeed=4.3` and the old triangle `{'safetyFactor': 0.4, 'slopeFactor': 0.3, 'timeFactor': 0.3}`. Central-Amsterdam trips print km/min/detour. The edge trips (`Centraal -> Amstelveen Stadshart`, `Diemen Sniep -> Vondelpark`) are EXPECTED to fail or produce truncated/absurd routes on the old clipped graph — a nonzero exit here is fine and is exactly the evidence we want. `tee` preserves the output either way.

- [ ] **Step 3: Commit the baseline**

```bash
git add docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt
git commit -m "test: capture golden-trip baseline on old graph and params"
```

---

### Task 3: Time-dominant bike triangle + realistic bike speed (TDD)

**Files:**
- Modify: `app/clients/otp.py:35` (`_BIKE_SPEED`) and `app/clients/otp.py:39` (`_BIKE_TRIANGLE`)
- Test: `tests/unit/test_otp.py:198` (`test_plan_sends_tuned_routing_params`) and `tests/unit/test_otp.py:202-226` (`test_bike_triangle_only_for_bike_modes`)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `_BIKE_SPEED == 4.7` and `_BIKE_TRIANGLE == {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}` in `app/clients/otp.py`, which the benchmark script (Task 1) picks up automatically for Task 5.

- [ ] **Step 1: Update the test expectations (failing first)**

In `tests/unit/test_otp.py`, change line 198:

```python
    assert variables["bikeSpeed"] == 4.7
```

and replace both triangle assertions (lines 211 and 219):

```python
    assert variables["triangle"] == {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}
```

The `optimize == "TRIANGLE"` assertions and the transit-only `is None` assertions stay unchanged.

- [ ] **Step 2: Run the two tests to verify they fail**

Run: `uv run pytest tests/unit/test_otp.py::test_plan_sends_tuned_routing_params tests/unit/test_otp.py::test_bike_triangle_only_for_bike_modes -v`
Expected: both FAIL on the new expected values (4.7 vs 4.3, new triangle vs old).

- [ ] **Step 3: Update the constants**

In `app/clients/otp.py`, change line 35 and line 39 (and the comment context above them):

```python
_BIKE_SPEED = 4.7
```

```python
# Time-dominant: direct routes like a local rides. Residual safety weight keeps the
# fietspad preference over parallel car roads; slope is irrelevant in flat Amsterdam.
_BIKE_TRIANGLE = {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}
```

- [ ] **Step 4: Run the full unit suite**

Run: `uv run pytest tests/unit -q`
Expected: all pass. If any other test hard-codes `4.3` or the old triangle, update it to the new values (a repo grep for `4.3` found only `test_otp.py:198` and `otp.py:35`, so none are expected).

- [ ] **Step 5: Commit**

```bash
git add app/clients/otp.py tests/unit/test_otp.py
git commit -m "feat: time-dominant bike triangle and realistic bike speed"
```

---

### Task 4: Bike+ferry for IJ crossings (TDD) + config alignment

The baseline showed pure-bike IJ crossings detour 2.6x because mode `BICYCLE` excludes
ferries. A real Amsterdam cyclist rolls the bike onto the free GVB ferry. The bike query
becomes `BICYCLE,FERRY`, and an itinerary whose only transit mode is FERRY still counts
as kind "bike".

**Files:**
- Modify: `app/clients/otp.py` (add `BIKE_MODES` constant near the other tuned constants, after line 39)
- Modify: `app/services/planner.py:26` (`_MODE_SETS`)
- Modify: `app/services/scoring.py:34-48` (`classify_kind`)
- Modify: `app/services/snap.py:56-58` (`_bike` query mode)
- Modify: `otp/scripts/golden_trips.py` (bike query uses `BIKE_MODES`; add a modes column)
- Modify: `otp/router-config.json` (align defaults: speed 4.7, numItineraries 5)
- Test: `tests/unit/test_scoring.py`, `tests/unit/test_planner.py`, `tests/unit/test_snap.py`

**Interfaces:**
- Consumes: `_to_transport_modes(mode: str)` and the constants in `app/clients/otp.py`.
- Produces: `BIKE_MODES = "BICYCLE,FERRY"` importable from `app.clients.otp`; `classify_kind` returning `"bike"` for bike+ferry-only itineraries. Task 5 relies on the golden script printing a `modes` column to prove FERRY legs are used.

- [ ] **Step 1: Write the failing classification tests**

In `tests/unit/test_scoring.py`, after the existing `test_classify_kind_by_legs` (line 71), add (using the file's existing `leg`/`itin` helpers):

```python
def test_classify_bike_plus_ferry_stays_bike():
    # A cyclist rolls the bike onto the GVB ferry: still a bike trip, not bike_and_ride.
    ferry_bike = itin(
        leg("BICYCLE", (14, 0), (14, 8)),
        leg("FERRY", (14, 8), (14, 14)),
        leg("BICYCLE", (14, 14), (14, 25)),
    )
    assert classify_kind(ferry_bike) == "bike"


def test_classify_bike_plus_ferry_plus_tram_is_bike_and_ride():
    mixed = itin(
        leg("BICYCLE", (14, 0), (14, 8)),
        leg("FERRY", (14, 8), (14, 14)),
        leg("TRAM", (14, 14), (14, 25), route="13"),
    )
    assert classify_kind(mixed) == "bike_and_ride"


def test_classify_ferry_without_bike_is_transit():
    ferry_walk = itin(leg("WALK", (14, 0), (14, 5)), leg("FERRY", (14, 5), (14, 15)))
    assert classify_kind(ferry_walk) == "transit"
```

- [ ] **Step 2: Write the failing mode-set tests**

In `tests/unit/test_planner.py`, the `FakeOTP` records the mode string of every call in
`self.calls`. Add a test that the bike fan-out query includes FERRY:

```python
async def test_bike_query_includes_ferry():
    # Pure-bike trips must be able to use GVB ferries for IJ crossings.
    fake = FakeOTP(by_mode={})
    await gather_candidates(fake, FROM, TO)
    assert BIKE_MODES == "BICYCLE,FERRY"
    assert BIKE_MODES in fake.calls
```

with `from app.clients.otp import BIKE_MODES` added to the imports (adjust `FROM`/`TO` to
whatever origin/destination constants the file already uses; if it uses literals, reuse
those literals).

In `tests/unit/test_snap.py`, the fake OTP's `plan()` receives `mode`. Extend the
existing fake (or the relevant assertion) so a snapped probe's mode is asserted to be
`BIKE_MODES`, e.g. record `mode` in the fake and assert
`all(m == BIKE_MODES for m in fake.modes)` in the first successful-snap test. Import
`BIKE_MODES` the same way.

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/test_scoring.py tests/unit/test_planner.py tests/unit/test_snap.py -q`
Expected: the three classification tests fail (`classify_kind` returns "bike_and_ride"
for bike+ferry), the planner/snap tests fail on import (`BIKE_MODES` does not exist yet).

- [ ] **Step 4: Implement**

In `app/clients/otp.py`, after the `_BIKE_TRIANGLE` line:

```python
# Pure-bike trips may use GVB ferries (free, bikes roll on) for IJ crossings; FERRY-only
# transit keeps kind "bike" in classify_kind. Shared by planner, snap, and the benchmark.
BIKE_MODES = "BICYCLE,FERRY"
```

In `app/services/planner.py` line 26 (import `BIKE_MODES` from `app.clients.otp`):

```python
_MODE_SETS = (BIKE_MODES, "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK")
```

and update the module docstring's "BICYCLE" mention to "BICYCLE+FERRY".

In `app/services/scoring.py`, replace `classify_kind`:

```python
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
```

In `app/services/snap.py`, import `BIKE_MODES` from `app.clients.otp`, change line 58 to
`mode=BIKE_MODES`, and update the `_bike` docstring ("One bike (BICYCLE+FERRY) query...")
plus the module docstring's "zero BICYCLE itineraries" phrasing if it now reads wrong.
(`_first_bike` needs no change: it already filters via `classify_kind == "bike"`, and a
ferry-only itinerary now classifies as "transit", so it can never be returned as a bike
snap.)

In `otp/scripts/golden_trips.py`:
- add `BIKE_MODES` and `_to_transport_modes` to the `app.clients.otp` import
- in `plan_bike`, replace `"modes": [{"mode": "BICYCLE"}]` with `"modes": _to_transport_modes(BIKE_MODES)`
- add a modes column so ferry use is visible in the evidence: in `main()`, after
  computing `detour`, add

```python
            leg_modes = ",".join(dict.fromkeys(leg["mode"] for leg in itin["legs"]))
```

and print it as a final column (widen the header line accordingly:
`print(f"{'trip':<34} {'km':>7} {'min':>6} {'detour':>7}  modes")` and
`print(f"{name:<34} {distance_m / 1000:>7.2f} {minutes:>6.1f} {detour:>7.2f}  {leg_modes}")`).

- [ ] **Step 5: Update existing tests that key on the old mode string**

`tests/unit/test_planner.py` uses `"BICYCLE"` as a `by_mode` dict key and in fake
`plan()` conditionals (e.g. around lines 106-125); replace those occurrences with
`BIKE_MODES` so the fakes answer the new bike query. Do NOT touch `"BICYCLE,TRANSIT,WALK"`.
Leg-level `leg("BICYCLE")` fixtures stay unchanged (leg modes are not query modes).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/unit -q`
Expected: all pass. Also run `uv run ruff check .` — clean.

- [ ] **Step 7: Align `otp/router-config.json`**

```json
{
  "routingDefaults": {
    "numItineraries": 5,
    "bicycle": { "speed": 4.7 }
  }
}
```
(Defaults only — the client sends explicit values per request; no graph rebuild needed.)

- [ ] **Step 8: Commit**

```bash
git add app/clients/otp.py app/services/planner.py app/services/scoring.py app/services/snap.py otp/scripts/golden_trips.py otp/router-config.json tests/unit/test_scoring.py tests/unit/test_planner.py tests/unit/test_snap.py
git commit -m "feat: pure-bike trips may use GVB ferries for IJ crossings"
```

---

### Task 5: After capture + comparison evidence

**Files:**
- Create: `docs/superpowers/evidence/2026-07-02-golden-trips-after.txt`

**Interfaces:**
- Consumes: `otp/scripts/golden_trips.py` (Tasks 1+4), the baseline file (Task 2), new constants (Tasks 3+4), the already-running OTP on `:8080` (graph unchanged — the script sends all routing params per request, so no rebuild/restart is needed).
- Produces: committed after-output plus a verdict against the spec's expected outcomes.

- [ ] **Step 1: Capture the after run**

```bash
uv run python otp/scripts/golden_trips.py | tee docs/superpowers/evidence/2026-07-02-golden-trips-after.txt
```
Expected: header shows `bikeSpeed=4.7` and triangle `{'safetyFactor': 0.3, 'slopeFactor': 0.0, 'timeFactor': 0.7}`; exit code 0 (all eight trips return an itinerary).

- [ ] **Step 2: Compare against the baseline**

Run: `diff docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt docs/superpowers/evidence/2026-07-02-golden-trips-after.txt`

Check the spec's expected outcomes and note each in the commit message body:
1. The two IJ-crossing trips (`NDSM -> Osdorpplein`, baseline 21.81 km detour 2.58; `De Pijp -> Noorderpark`, baseline 11.54 km detour 2.61) now show `FERRY` in the modes column and a detour ratio well below 2 (expect roughly 1.2-1.5).
2. Edge trips (Amstelveen, Diemen) still succeed (coverage regression check).
3. No other trip's detour ratio got dramatically worse (> +0.1 vs baseline).
4. Durations are plausible for ~17 km/h riding: minutes roughly = km / 17 * 60 + small overhead.

If outcome 1 fails with NO ferry leg appearing on either IJ trip, the GVB GTFS likely lacks `bikes_allowed` on ferry trips: STOP and report (the spec marks the GTFS patch as out of scope — escalate, do not implement it). If outcome 3 or 4 fails, STOP and report — the constants may need adjustment; do not tune blindly without recording what was observed.

- [ ] **Step 3: Commit the evidence**

```bash
git add docs/superpowers/evidence/2026-07-02-golden-trips-after.txt
git commit -m "test: golden-trip evidence after routing efficiency changes"
```

- [ ] **Step 4: Full suite sanity**

Run: `uv run pytest tests/unit -q`
Expected: all pass (nothing in this task changed code, this is a final gate).
