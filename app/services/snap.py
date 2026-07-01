"""Snap a pedestrian-hub coordinate to a nearby bikeable point.

Big transit hubs (Bijlmer ArenA, elevated stations) sit on pedestrian decks with no
bike-traversable edge adjacent, so OTP returns zero BICYCLE itineraries when an endpoint
lands exactly on the deck — even though the surrounding streets bike fine and walking
routes there without trouble. OTP is not wrong (you cannot start a ride standing on the
deck); the input coordinate is just a pedestrian centroid.

This is a **fallback**, used only when the normal fan-out finds no pure-bike route: it
re-asks OTP with the offending endpoint nudged to nearby points (a small ring), modelling
the rider walking their bike the last ~200-450 m off the deck to a bikeable street. It
returns the first bike itinerary found, or None if snapping doesn't help (a genuinely
bike-unreachable location).
"""

from __future__ import annotations

from datetime import datetime

from app.clients.otp import Itinerary, OTPClient, OTPError
from app.services.scoring import classify_kind

# Offsets (dlat, dlon) probed around a stuck endpoint: a ~220 m ring, then a ~440 m ring.
# ~0.002 lat ≈ 220 m; ~0.0022 lon ≈ 150 m at Amsterdam's latitude. Ordered nearest-first
# so the returned ride stays as close to the real endpoint as possible.
_SNAP_RING: tuple[tuple[float, float], ...] = (
    (0.002, 0.0),
    (0.0, 0.0022),
    (-0.002, 0.0),
    (0.0, -0.0022),
    (0.002, 0.0022),
    (-0.002, 0.0022),
    (0.002, -0.0022),
    (-0.002, -0.0022),
    (0.004, 0.0),
    (0.0, 0.0044),
    (-0.004, 0.0),
    (0.0, -0.0044),
)


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
) -> Itinerary | None:
    """One BICYCLE query; None on OTPError or no bike itinerary (a failed probe is skipped)."""
    try:
        plan = await otp.plan(from_place=frm, to_place=to, mode="BICYCLE", departure=departure)
    except OTPError:
        return None
    return _first_bike(plan)


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
    offset first), or None if no probed point is bikeable.
    """
    for dlat, dlon in _SNAP_RING:
        itin = await _bike(otp, origin, (to[0] + dlat, to[1] + dlon), departure)
        if itin is not None:
            return itin
    for dlat, dlon in _SNAP_RING:
        itin = await _bike(otp, (origin[0] + dlat, origin[1] + dlon), to, departure)
        if itin is not None:
            return itin
    return None
