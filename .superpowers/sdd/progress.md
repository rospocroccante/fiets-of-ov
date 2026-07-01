# SDD Progress: Multimodal rain-aware routing (backend)

Branch: feat/multimodal-rain-aware-routing
Plan: docs/superpowers/plans/2026-06-30-multimodal-rain-aware-routing.md
Base: df99acc (plan commit)

Task 1: complete (commits df99acc..f4b29e8, review clean after 1 fix - added direct score() arithmetic test)
  Minor findings deferred to final review:
  - _leg_rain uses closed interval start<=t<=end (plan-mandated; consistent with decide()/_rain_summary)
  - exposed-minutes estimate coarse (5-min granularity; reviewer: acceptable)
  - rain_minutes float accumulator vs int dataclass field (round() returns int; cosmetic)
  - classify_kind walk+transit-no-bike boundary not directly unit-tested (covered indirectly)
Task 2: complete (commits f4b29e8..7b10c51, review clean after 1 fix - chained OTPError on total failure)
  Minor deferred: single-element kind ordering assertion vs sorted() elsewhere; itin() fixture end_time derived from duration (unused in asserts)
Task 3: complete (commits 7b10c51..984dad1, review clean after 1 fix - peak None-guard)
  Minor deferred: _rain_summary inclusive end interval (preserved from old decide; consistent); TRANSIT fixture 14min vs old 12min (internally consistent); one rain test lacks rain_expected assert; _reason no-forecast dict KeyError if OptionKind grows (3 fixed values today)
Task 4: complete (commit 984dad1..bf7c820, review clean, no fixes; Minor: OptionKind alias placement mid-file - stylistic)
Task 5: complete (commit bf7c820..5925fd4, review clean, no fixes)
  Minor deferred: advice.py get_advice Query params lost description= (brief-consistent, cosmetic OpenAPI); _minutes duplicated in advice.py+plan.py (brief-permitted)
Task 6: complete (commit 5925fd4..f419bee, review clean, no fixes; notify 11/11). test_notify.py unchanged (stub tolerates the 3rd mode via partial-failure path).
  Minor deferred: evaluate_trip_alert docstring lost Args/Returns section
Task 7: complete (commits f419bee..969f2e6, review clean after 1 fix - tightened geocode assert + restored "tram 13" route checks). FULL SUITE 130 passed, ruff clean.
  BEHAVIORAL NOTE for final summary: dry/no-forecast day now ranks by travel time (fastest wins), so transit can outrank bike on a dry day - intended consequence of generalized-cost model (was "dry->always bike" before). Surface to user as a tuning question (bike-preference bonus?).
  Minor deferred: test_plan geometry test uses set-membership recommendation assert (non-deterministic under rain; acceptable)
=== BACKEND COMPLETE (Tasks 1-7) ===

=== USER-AUTHORED CHANGE (mid-run) ===
Spec updated + bias implemented BY THE USER directly (not via subagent):
  7b172cf docs: record bike-preference (transit must beat bike by >10min when dry)
  0fb2772 feat: bike-preference so dry-day routing stays bike-first
  -> scoring.py Weights.transit_bias_min=10.0; cost += bias when kind!="bike".
  -> test_scoring.py + test_advice_endpoint.py expectations updated (dry->bike, rain->transit). Verified: unit suite 96 passed, ruff clean.
  -> Will be covered by the final whole-branch review. NOTE: stray untracked uv.lock appeared (from `uv run` in subagents) - decide gitignore/commit at finish.
Task 7: complete (commits f419bee..d2a0c98 endpoint tests; implementer process crashed before report but commit landed; controller verified 130 passed + ruff clean)
  Review found Needs-fixes: 3 weakened assertions. Resolved together with bike-preference change.
Bike-preference (user decision: bike-first unless transit saves >~10min): commit 7b172cf (spec) + 0fb2772 (scoring.py transit_bias_min=10 + scoring tests + restored/tightened Task7 assertions). Re-review APPROVED. Full suite 131 passed, ruff clean.
  This amended Task 1's scoring.py (Weights.transit_bias_min, score() bias term).
  Minor deferred: scoring.py:34 comment says "transit" but bias applies to all non-bike; partial-rain test comment doesn't mention bias.
BACKEND COMPLETE through Task 7. HEAD=0fb2772, 131 passed.

Task 12 (e2e): VERIFIED against live OTP (started current backend on :8001).
- /v1/advice + /v1/plan return the new ranked options shape.
- Centraal->Vondelpark (dry): recommendation=bike ("dry during your 24-min ride -> bike"), bike(24.4) ranked above transit(46.5). Bike-preference confirmed.
- Centraal->Bijlmer (raining ~0.2mm/h now): recommendation=transit. Rain-flip confirmed.
- Graceful degradation confirmed: OTP returns 0 BICYCLE itineraries for Centraal->Bijlmer (~10km); plan shows transit-only (old code would 502).
FINDINGS for final review:
- (FIX) app/api/advice.py: bike_minutes falls back to options[0].duration when no bike candidate -> reports e.g. 28 "bike_minutes" that is actually transit's. Should be None. Needs AdviceResponse.bike_minutes: int|None + frontend Advice.bike_minutes: number|null.
- (INFRA, out of feature scope) OTP returns no bike route for some medium Amsterdam trips (Centraal->Bijlmer): router-config bike limits worth tuning; not this feature's code.

=== FINAL REVIEW (both repos): READY, zero Critical/Important ===
Backend fix wave: 3c9bef3 refactor: drop dead first_transit_itinerary + tidy _reason. Final: 128 passed, ruff clean.
Open items for human at finish:
- uv.lock untracked (reviewer recommends committing for an app repo) - human decision
- .superpowers/ untracked in backend (gitignored in frontend) - add to backend .gitignore or leave
- dead advice path in frontend (score.ts/getAdvice/mockAdviceFor) now unused in prod - optional follow-up cleanup
- /v1/advice no longer 502s on "no bike route" (returns transit/bike_and_ride) - intentional parity change, note in PR/changelog
- Task 12 live-OTP browser e2e is a MANUAL step (docker compose -f docker-compose.yml -f docker-compose.otp.yml up; npm run dev) - not run in this session

Final whole-branch review (opus, both repos): READY AFTER FIXES. One required fix (Issue #1 bike_minutes nullable, cross-repo) + minors.
Final fix wave: backend 4359b65 (bike_minutes int|None + drop misleading fallback + _reason .get() + notify docstring + uv.lock gitignore + new no-bike advice test), frontend 50e0354 (Advice.bike_minutes number|null).
FINAL GREEN (controller-verified): backend 94 unit + 35 integration = 129 passed, ruff clean; frontend tsc clean, 53 passed, build ok.
Deferred follow-ups (accepted by final review): closed-interval rain window; bikeMinutes 0-leg defensive; OTP returns no bike route for some ~10km Amsterdam trips (router-config tuning, infra/out-of-scope).
READY TO MERGE. Backend HEAD=4359b65, frontend HEAD=50e0354.

## Part 2: pedestrian-hub bike snapping (systematic-debugging)
Root cause: NOT distance/coverage/config. OTP correctly returns 0 BICYCLE itineraries when an endpoint lands on a pedestrian deck (e.g. Bijlmer ArenA exact coord); dead zone is ~150m wide, everywhere else bikes fine, walk routes fine, no routingError. OSM bbox huge (4.56,52.03)-(5.21,52.51).
Fix: app/services/snap.py bike_with_snapping() - fallback ring probe (~220m/440m, nearest-first) re-asking OTP with the stuck endpoint nudged; integrated in gather_candidates only when no bike candidate but trip routable. Fallback-only (no extra calls common path).
Verified: 134 tests pass (+5), ruff clean; LIVE Centraal->Bijlmer ArenA now returns bike(49min)+transit(27min) - previously transit-only.
Commit 74ab13e on feat/multimodal-rain-aware-routing (pushed, updates PR #1).
