"""Snap a pedestrian-hub coordinate to a nearby bikeable point.

Big transit hubs (Bijlmer ArenA, elevated stations) sit on pedestrian decks with no
bike-traversable edge adjacent, so OTP returns zero bikeable itineraries when an endpoint
lands exactly on the deck — even though the surrounding streets bike fine and walking
routes there without trouble. OTP is not wrong (you cannot start a ride standing on the
deck); the input coordinate is just a pedestrian centroid.

This is a **fallback**, used only when the normal fan-out finds no pure-bike route: it
re-asks OTP with the offending endpoint nudged to nearby points (small rings), modelling
the rider walking their bike the last ~200-450 m off the deck to a bikeable street.

Cost control, since this can mean up to 24 speculative queries against a live OTP:
each ring's offsets are probed concurrently with a slim existence query (few itineraries,
no geometry/steps), the first offset that routes — in the ring's defined nearest-first
order, not answer order — is re-planned in full so the result still draws on a map, and
the whole fallback runs under `snap_timeout_seconds`. On deadline it returns None, the
same "no bike option" answer as a genuinely bike-unreachable location.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.clients.otp import BIKE_MODES, Itinerary, OTPClient, OTPError
from app.core.config import get_settings
from app.services.scoring import classify_kind

# Offsets (dlat, dlon) probed around a stuck endpoint, grouped in rings probed nearest
# ring first: ~220 m out, then ~440 m. ~0.002 lat = 220 m; ~0.0022 lon = 150 m at
# Amsterdam's latitude. Within a ring the offset order defines which hit wins, keeping
# the returned ride as close to the real endpoint as possible.
_SNAP_RINGS: tuple[tuple[tuple[float, float], ...], ...] = (
    (
        (0.002, 0.0),
        (0.0, 0.0022),
        (-0.002, 0.0),
        (0.0, -0.0022),
        (0.002, 0.0022),
        (-0.002, 0.0022),
        (0.002, -0.0022),
        (-0.002, -0.0022),
    ),
    (
        (0.004, 0.0),
        (0.0, 0.0044),
        (-0.004, 0.0),
        (0.0, -0.0044),
    ),
)

# Probes only ask "does a bike itinerary exist here?". Not 1: a BICYCLE+FERRY plan can
# lead with walk/ferry itineraries that classify as non-bike, so keep a little headroom.
_PROBE_NUM_ITINERARIES = 3


def _first_bike(plan) -> Itinerary | None:
    """The first pure-bike itinerary in a plan, or None."""
    for itin in plan.itineraries:
        if classify_kind(itin) == "bike":
            return itin
    return None


async def _bike(
    otp: OTPClient,
    frm: tuple[float, float],
    to: tuple[float, float],
    departure: datetime | None,
    slim: bool = False,
) -> Itinerary | None:
    """One bike (BICYCLE+FERRY) query; None on OTPError or no bike itinerary (a failed
    probe is skipped). `slim` asks for the cheap existence-probe variant."""
    try:
        plan = await otp.plan(
            from_place=frm,
            to_place=to,
            mode=BIKE_MODES,
            departure=departure,
            num_itineraries=_PROBE_NUM_ITINERARIES if slim else None,
            slim=slim,
        )
    except OTPError:
        return None
    return _first_bike(plan)


async def _snap_one_endpoint(
    otp: OTPClient,
    origin: tuple[float, float],
    to: tuple[float, float],
    departure: datetime | None,
    snap_destination: bool,
) -> Itinerary | None:
    """Probe the snap rings around one endpoint, returning a full-detail bike itinerary
    for the best offset that routes, or None.

    Rings are tried nearest-first; within a ring all offsets are probed concurrently
    with slim queries. asyncio.gather preserves input order, so the winner is the first
    offset in the ring's defined order that routed — not whichever answered fastest.
    """
    for ring in _SNAP_RINGS:
        if snap_destination:
            pairs = [(origin, (to[0] + dlat, to[1] + dlon)) for dlat, dlon in ring]
        else:
            pairs = [((origin[0] + dlat, origin[1] + dlon), to) for dlat, dlon in ring]
        probes = await asyncio.gather(
            *(_bike(otp, frm, dest, departure, slim=True) for frm, dest in pairs)
        )
        for (frm, dest), hit in zip(pairs, probes, strict=True):
            if hit is None:
                continue
            # Re-plan the winning offset in full so the returned itinerary carries the
            # geometry/steps the probe skipped; if the re-plan finds no bike route after
            # all (OTP hiccup), keep scanning rather than give up.
            itin = await _bike(otp, frm, dest, departure)
            if itin is not None:
                return itin
    return None


async def bike_with_snapping(
    otp: OTPClient,
    origin: tuple[float, float],
    to: tuple[float, float],
    departure: datetime | None = None,
) -> Itinerary | None:
    """Find a pure-bike itinerary when the direct query returned none, by nudging an
    endpoint off a pedestrian deck.

    Snaps the destination first (the common hub case) holding the real origin, then the
    origin holding the real destination. Returns the first bike itinerary found (nearest
    ring first, ring offsets probed concurrently), or None if no probed point is
    bikeable or the `snap_timeout_seconds` deadline expires first.
    """
    try:
        async with asyncio.timeout(get_settings().snap_timeout_seconds):
            for snap_destination in (True, False):
                itin = await _snap_one_endpoint(otp, origin, to, departure, snap_destination)
                if itin is not None:
                    return itin
    except TimeoutError:
        # Best-effort fallback: a slow OTP must not hold the request hostage for up to
        # 24 routing queries. Past the deadline, "no bike option" is the honest answer.
        return None
    return None
