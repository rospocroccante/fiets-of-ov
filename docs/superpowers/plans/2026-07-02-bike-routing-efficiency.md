# Bike Routing Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Direct (time-dominant) bike routes, full-Amsterdam graph coverage, and realistic bike time estimates, with before/after golden-trip evidence.

**Architecture:** All changes in the backend repo `/Users/Rospo/Vibecoding/fiets-of-ov`. Routing itself stays in OTP2; we change the client-sent bike parameters (`app/clients/otp.py`), swap the OSM extract feeding the graph (`otp/`), and add a standalone benchmark script that talks to the live OTP GraphQL API.

**Tech Stack:** Python 3.12, httpx, pytest + respx (offline GraphQL mocks), uv for env/commands, OTP 2.6.0 (Java 21), Geofabrik OSM extracts.

**Spec:** `docs/superpowers/specs/2026-07-02-bike-routing-efficiency-design.md`

## Global Constraints

- Working directory for every command: `/Users/Rospo/Vibecoding/fiets-of-ov` (backend repo). NOT the frontend repo.
- Branch: `feat/amsterdam-pathfinding` (continue it; do not branch off).
- Run tests with: `uv run pytest tests/unit -q`.
- No emoji anywhere (code, docs, commits). No AI/Claude trailers in commit messages.
- New bike constants (exact values): `_BIKE_SPEED = 4.7`, `_BIKE_TRIANGLE = {"safetyFactor": 0.3, "slopeFactor": 0.0, "timeFactor": 0.7}`.
- New OSM source filename (exact): `noord-holland-latest.osm.pbf` from `https://download.geofabrik.de/europe/netherlands/noord-holland-latest.osm.pbf`.
- Task order matters: the baseline capture (Task 2) MUST run before the parameter change (Task 3), because the benchmark script imports the live constants from `app/clients/otp.py`.

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

### Task 4: Province-wide OSM extract + config alignment + rebuild

**Files:**
- Modify: `otp/build-config.json` (osm source)
- Modify: `otp/router-config.json` (speed, numItineraries)
- Modify: `otp/README.md` (data table, notes)
- Modify: `otp/scripts/run_otp.sh:5` (prereq comment)
- Modify: `docker-compose.otp.yml:10` (prereq comment)
- Rebuilt artifact (NOT committed, gitignored): `otp/data/graph.obj`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: a live OTP on `:8080` serving the Noord-Holland graph, which Task 5 benchmarks against.

- [ ] **Step 1: Update `otp/build-config.json`**

```json
{
  "transitFeeds": [
    { "type": "gtfs", "source": "gtfs-gvb.zip" }
  ],
  "osm": [
    { "source": "noord-holland-latest.osm.pbf" }
  ]
}
```

- [ ] **Step 2: Update `otp/router-config.json`**

```json
{
  "routingDefaults": {
    "numItineraries": 5,
    "bicycle": { "speed": 4.7 }
  }
}
```

- [ ] **Step 3: Update the docs and comments**

In `otp/README.md`, replace the `amsterdam.osm.pbf` table row with:

```markdown
| `noord-holland-latest.osm.pbf` | https://download.geofabrik.de/europe/netherlands/noord-holland-latest.osm.pbf |
```

and replace the final "Notes" bullet about coverage with:

```markdown
- Coverage is GVB transit within the Noord-Holland OSM extract (whole gemeente Amsterdam
  plus Amstelveen, Diemen, Ouder-Amstel, Zaandam and beyond). The provincial extract
  replaced the clipped BBBike Amsterdam bbox that cut off the city edges.
```

In `otp/scripts/run_otp.sh` line 5, change the prereq comment to name `noord-holland-latest.osm.pbf` instead of `amsterdam.osm.pbf`. In `docker-compose.otp.yml` line 10, same comment change.

- [ ] **Step 4: Download the extract**

```bash
curl -L --fail -o otp/data/noord-holland-latest.osm.pbf \
  https://download.geofabrik.de/europe/netherlands/noord-holland-latest.osm.pbf
```
Expected: ~130 MB file. Verify: `ls -lh otp/data/noord-holland-latest.osm.pbf` shows > 100 MB.

- [ ] **Step 5: Stop the old OTP, rebuild the graph**

Stop any running OTP first (`pkill -f otp-2.6.0-shaded.jar` or stop the docker service), then:

```bash
rm -f otp/data/graph.obj
cp otp/build-config.json otp/data/build-config.json
cp otp/router-config.json otp/data/router-config.json
java -Xmx8g -jar otp/data/otp-2.6.0-shaded.jar --build --save otp/data
```
Expected: build completes (provincial extract: expect ~10-25 min) and writes a new `otp/data/graph.obj`. If the JVM exits with OOM, retry with `-Xmx12g`.

- [ ] **Step 6: Serve the new graph**

```bash
nohup otp/scripts/run_otp.sh > /tmp/otp-new.log 2>&1 &
```
Poll until ready: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/otp/gtfs/v1 -X POST -H 'Content-Type: application/json' -d '{"query":"{__typename}"}'` returns `200`.

- [ ] **Step 7: Commit (config + docs only; data files are gitignored)**

```bash
git add otp/build-config.json otp/router-config.json otp/README.md otp/scripts/run_otp.sh docker-compose.otp.yml
git commit -m "feat: Noord-Holland OSM extract and aligned router defaults"
```

---

### Task 5: After capture + comparison evidence

**Files:**
- Create: `docs/superpowers/evidence/2026-07-02-golden-trips-after.txt`

**Interfaces:**
- Consumes: `otp/scripts/golden_trips.py` (Task 1), the baseline file (Task 2), new constants (Task 3), live OTP with the new graph (Task 4).
- Produces: committed after-output plus a verdict against the spec's expected outcomes.

- [ ] **Step 1: Capture the after run**

```bash
uv run python otp/scripts/golden_trips.py | tee docs/superpowers/evidence/2026-07-02-golden-trips-after.txt
```
Expected: header shows `bikeSpeed=4.7` and triangle `{'safetyFactor': 0.3, 'slopeFactor': 0.0, 'timeFactor': 0.7}`; exit code 0 (ALL eight trips return an itinerary, including Amstelveen and Diemen).

- [ ] **Step 2: Compare against the baseline**

Run: `diff docs/superpowers/evidence/2026-07-02-golden-trips-baseline.txt docs/superpowers/evidence/2026-07-02-golden-trips-after.txt`

Check the spec's three expected outcomes and note each in the commit message body:
1. Edge trips (Amstelveen, Diemen) now succeed (baseline: failed or absurd).
2. Detour ratio per trip is lower than or equal to baseline on trips where the old triangle detoured; no trip's detour ratio got dramatically worse (> +0.1).
3. Durations are plausible for ~17 km/h riding: minutes roughly = km / 17 * 60 + small overhead. Spot-check two trips against Google Maps bike estimates by hand if available; otherwise the km/17 sanity check suffices.

If outcome 2 or 3 fails, STOP and report — the constants may need adjustment (they are single documented values in `app/clients/otp.py`); do not tune blindly without recording what was observed.

- [ ] **Step 3: Commit the evidence**

```bash
git add docs/superpowers/evidence/2026-07-02-golden-trips-after.txt
git commit -m "test: golden-trip evidence after routing efficiency changes"
```

- [ ] **Step 4: Full suite sanity**

Run: `uv run pytest tests/unit -q`
Expected: all pass (nothing in this task changed code, this is a final gate).
