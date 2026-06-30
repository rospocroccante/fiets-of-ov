"""Candidate generation: ask OTP for bike, transit, and bike-and-ride options at once.

The single source of routing candidates for `/v1/plan`, `/v1/advice`, and the rain-alert
notifier. It fans out three OTP queries concurrently — BICYCLE, TRANSIT+WALK, and
BICYCLE+TRANSIT+WALK — pools every itinerary, classifies each by its leg modes, drops
walk-only trips, and keeps the single shortest itinerary per kind. OTP does the real
multimodal routing; this orchestrates and de-duplicates.

Resilience matches the rest of the service: a failed query is dropped (its kind is simply
absent); only when *every* query fails do we raise `OTPError`, so the caller surfaces a
clear 502 instead of a fabricated route.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.clients.otp import Itinerary, OTPClient, OTPError
from app.services.scoring import Candidate, OptionKind, classify_kind

# The mixed set yields bike-and-ride; the pure sets guarantee a clean bike baseline (the
# rain summary needs it) and a transit option even when the mixed plan omits them.
_MODE_SETS = ("BICYCLE", "TRANSIT,WALK", "BICYCLE,TRANSIT,WALK")


async def gather_candidates(
    otp: OTPClient,
    from_place: tuple[float, float],
    to_place: tuple[float, float],
    departure: datetime | None = None,
) -> list[Candidate]:
    """Return the best candidate per kind for `from_place` -> `to_place`.

    Runs the three OTP queries concurrently. Raises `OTPError` only if *all* fail.
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
        raise OTPError(f"all OTP queries failed: {first_error}")

    best: dict[OptionKind, Itinerary] = {}
    for plan in plans:
        for itin in plan.itineraries:
            kind = classify_kind(itin)
            if kind is None:
                continue
            current = best.get(kind)
            if current is None or itin.duration < current.duration:
                best[kind] = itin

    return [Candidate(kind=kind, itinerary=itin) for kind, itin in best.items()]
