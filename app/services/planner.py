"""Candidate generation: ask OTP for bike, transit, and bike-and-ride options at once.

The single source of routing candidates for `/v1/plan`, `/v1/advice`, and the rain-alert
notifier. It fans out three OTP queries concurrently — BICYCLE+FERRY, TRANSIT+WALK, and
BICYCLE+TRANSIT+WALK — pools every itinerary, classifies each by its leg modes, and drops
walk-only trips. It returns every classified candidate; the per-kind winner is chosen later
by generalized cost in `scoring.rank`. OTP does the real multimodal routing; this
orchestrates.

Resilience matches the rest of the service: a query that fails with `OTPError` is dropped
(its kind is simply absent) but logged, so a silently missing kind stays diagnosable; only
when *every* query fails do we raise `OTPError`, so the caller surfaces a clear 502
instead of a fabricated route. Anything that is not an `OTPError` is a programming error
and propagates immediately rather than masquerading as "no transit".
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from app.clients.otp import BIKE_MODES, Itinerary, OTPClient, OTPError, Plan
from app.core.cache import Cache
from app.services.scoring import Candidate, classify_kind
from app.services.snap import bike_with_snapping

logger = logging.getLogger(__name__)

# The mixed set yields bike-and-ride; the pure sets guarantee a clean bike baseline (the
# rain summary needs it) and a transit option even when the mixed plan omits them.
_MODE_SETS = (BIKE_MODES, "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK")

# Identical from/to requests within this window reuse the previous OTP answer instead of
# re-running the 3-query fan-out. Plans are now-anchored, so a cached plan drifts by at
# most the TTL; 45 s is well under transit departure granularity while absorbing bursts
# (double submits, both endpoints called for the same trip).
_PLAN_CACHE_TTL_SECONDS = 45


async def gather_candidates(
    otp: OTPClient,
    from_place: tuple[float, float],
    to_place: tuple[float, float],
    departure: datetime | None = None,
) -> list[Candidate]:
    """Return every classified candidate for `from_place` -> `to_place`.

    Runs the three OTP queries concurrently and pools all itineraries (walk-only dropped);
    the lowest-cost per kind is selected later in `scoring.rank`. Raises `OTPError` only if
    *all* queries fail.
    """
    results = await asyncio.gather(
        *(
            otp.plan(from_place=from_place, to_place=to_place, mode=mode, departure=departure)
            for mode in _MODE_SETS
        ),
        return_exceptions=True,
    )

    plans: list[Plan] = []
    first_error: OTPError | None = None
    for mode, result in zip(_MODE_SETS, results, strict=True):
        if isinstance(result, OTPError):
            # An upstream failure on one mode set degrades that kind, not the request —
            # but log it, or "transit silently missing" is undiagnosable in production.
            logger.warning("OTP query for mode set %s failed: %s", mode, result)
            if first_error is None:
                first_error = result
            continue
        if isinstance(result, BaseException):
            # Anything else is a programming error (or a CancelledError, which is never
            # an OTPError) and must not masquerade as "no transit": re-raise it.
            raise result
        plans.append(result)
    if not plans:
        raise OTPError(f"all OTP queries failed: {first_error}") from first_error

    candidates: list[Candidate] = []
    for plan in plans:
        for itin in plan.itineraries:
            kind = classify_kind(itin)
            if kind is None:
                continue
            candidates.append(Candidate(kind=kind, itinerary=itin))

    # Pedestrian-deck fallback: the trip routes (transit/walk found) but no pure-bike
    # itinerary came back — the origin/destination likely sits on a bike-disconnected
    # pedestrian hub. Nudge the stuck endpoint to a nearby bikeable point and retry, so a
    # bike option still shows. Only runs on this exception path, never on the common case.
    if not any(c.kind == "bike" for c in candidates) and candidates:
        snapped = await bike_with_snapping(otp, from_place, to_place, departure)
        if snapped is not None:
            candidates.append(Candidate(kind="bike", itinerary=snapped))

    return candidates


# --- short-TTL plan cache (fail-open, mirroring app/services/rain.py) ---------------


def _cache_key(
    from_place: tuple[float, float],
    to_place: tuple[float, float],
    departure: datetime | None,
) -> str:
    """Cache key for a trip, coordinates rounded to 4 decimals (~11 m).

    Rounding groups jittered re-requests of the same trip (map nudges, GPS drift) onto
    one entry while keeping distinct addresses apart. The departure joins the key so a
    pinned plan (e.g. a trip alert) can never answer a plan-from-"now" request.
    """
    when = departure.isoformat() if departure is not None else "now"
    return (
        f"plan:{from_place[0]:.4f},{from_place[1]:.4f}"
        f"->{to_place[0]:.4f},{to_place[1]:.4f}@{when}"
    )


async def _read_cached_candidates(cache: Cache, key: str) -> list[Candidate] | None:
    """Return cached candidates, or None on miss/outage/poison.

    The read, JSON decode, and schema validation sit inside one guard so a Redis outage
    or a stale/corrupt entry degrades to a cache miss, never a failed request.
    """
    try:
        raw = await cache.get(key)
        if not raw:
            return None
        return [
            Candidate(kind=entry["kind"], itinerary=Itinerary.model_validate(entry["itinerary"]))
            for entry in json.loads(raw)
        ]
    except Exception:
        logger.warning("plan cache read failed; treating as miss", exc_info=True)
        return None


async def _write_cached_candidates(cache: Cache, key: str, candidates: list[Candidate]) -> None:
    """Store candidates under the short TTL, ignoring any cache error (fail-open)."""
    payload = json.dumps(
        [{"kind": c.kind, "itinerary": c.itinerary.model_dump(mode="json")} for c in candidates]
    )
    try:
        await cache.set(key, payload, ttl_seconds=_PLAN_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("plan cache set failed; continuing without caching", exc_info=True)


async def gather_candidates_cached(
    otp: OTPClient,
    cache: Cache,
    from_place: tuple[float, float],
    to_place: tuple[float, float],
    departure: datetime | None = None,
) -> list[Candidate]:
    """`gather_candidates` behind a short-TTL, fail-open cache.

    Identical trips requested within `_PLAN_CACHE_TTL_SECONDS` (from both `/v1/plan` and
    `/v1/advice`, which share the key space) skip the 3-query OTP fan-out entirely.
    Freshness is delegated to the backend TTL — unlike the rain cache there is no
    stale-fallback tier, because a stale route is not better than recomputing one.
    Only successful, non-empty results are cached: a no-route or failed answer must be
    retried on the next request, not pinned for the TTL.
    """
    key = _cache_key(from_place, to_place, departure)
    cached = await _read_cached_candidates(cache, key)
    if cached is not None:
        return cached

    candidates = await gather_candidates(otp, from_place, to_place, departure=departure)
    if candidates:
        await _write_cached_candidates(cache, key, candidates)
    return candidates
