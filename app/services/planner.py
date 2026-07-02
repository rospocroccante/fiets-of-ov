"""Candidate generation: ask OTP for bike, transit, and bike-and-ride options at once.

The single source of routing candidates for `/v1/plan`, `/v1/advice`, and the rain-alert
notifier. It fans out three OTP queries concurrently — BICYCLE+FERRY, TRANSIT+WALK, and
BICYCLE+TRANSIT+WALK — pools every itinerary, classifies each by its leg modes, and drops
walk-only trips. It returns every classified candidate; the per-kind winner is chosen later
by generalized cost in `scoring.rank`. OTP does the real multimodal routing; this
orchestrates.

Resilience matches the rest of the service: a failed query is dropped (its kind is simply
absent); only when *every* query fails do we raise `OTPError`, so the caller surfaces a
clear 502 instead of a fabricated route.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.clients.otp import BIKE_MODES, OTPClient, OTPError
from app.services.scoring import Candidate, classify_kind
from app.services.snap import bike_with_snapping

# The mixed set yields bike-and-ride; the pure sets guarantee a clean bike baseline (the
# rain summary needs it) and a transit option even when the mixed plan omits them.
_MODE_SETS = (BIKE_MODES, "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK")


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

    plans = [r for r in results if not isinstance(r, BaseException)]
    if not plans:
        first_error = next((r for r in results if isinstance(r, BaseException)), None)
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
