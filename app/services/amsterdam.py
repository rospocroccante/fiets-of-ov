"""Amsterdam local-knowledge module — pure helpers, no I/O.

Encodes curated geographic knowledge about Amsterdam's transit network: major
interchange hubs, GVB ferry crossings, and the bike-to-station (bike-and-ride)
pattern. All functions are side-effect-free and fully unit-testable offline.

Consumes:
    app.clients.otp.Itinerary, app.clients.otp.Leg
    app.services.geo.haversine_m(lat1, lon1, lat2, lon2) -> float  (metres)
"""

from app.clients.otp import Itinerary
from app.services.geo import haversine_m

# Radius within which a point is considered "at" a major hub.
HUB_RADIUS_M: float = 250.0

# Curated list of Amsterdam major interchange stations.  Each entry is
# (human-readable name, WGS84 latitude, WGS84 longitude).
MAJOR_HUBS: tuple[tuple[str, float, float], ...] = (
    ("Amsterdam Centraal", 52.3791, 4.9003),
    ("Amsterdam Zuid", 52.3389, 4.8730),
    ("Amstel", 52.3467, 4.9177),
    ("Sloterdijk", 52.3889, 4.8375),
    ("Lelylaan", 52.3576, 4.8378),
    ("Bijlmer ArenA", 52.3122, 4.9471),
    ("Duivendrecht", 52.3268, 4.9370),
    ("RAI", 52.3376, 4.8896),
    ("Muiderpoort", 52.3603, 4.9280),
)

# Leg modes that represent street (non-transit) movement; transit legs are those whose
# mode is not in this set.
_STREET_MODES: frozenset[str] = frozenset({"WALK", "BICYCLE"})


def is_near_hub(lat: float | None, lon: float | None) -> bool:
    """Return True when (lat, lon) is within HUB_RADIUS_M of any MAJOR_HUB.

    Returns False immediately when either coordinate is None so callers do not
    need to guard against optional coordinates.
    """
    if lat is None or lon is None:
        return False
    return any(
        haversine_m(lat, lon, h_lat, h_lon) <= HUB_RADIUS_M
        for _, h_lat, h_lon in MAJOR_HUBS
    )


def transfer_points(itinerary: Itinerary) -> list[tuple[float, float]]:
    """Return the (lat, lon) of each transfer point in the itinerary.

    A transfer occurs between consecutive transit boardings.  Transit legs are
    those whose mode is not in _STREET_MODES (i.e. not WALK or BICYCLE).  For
    each transit leg after the first transit leg, the from-coordinates of that
    leg are the transfer point — one point per transfer.  Legs with missing
    from-coordinates are silently omitted.
    """
    transit_legs = [leg for leg in itinerary.legs if leg.mode not in _STREET_MODES]
    points: list[tuple[float, float]] = []
    for leg in transit_legs[1:]:
        if leg.from_lat is not None and leg.from_lon is not None:
            points.append((leg.from_lat, leg.from_lon))
    return points


def has_ferry(itinerary: Itinerary) -> bool:
    """Return True when the itinerary contains at least one FERRY leg."""
    return any(leg.mode == "FERRY" for leg in itinerary.legs)


def bike_handoff_point(itinerary: Itinerary) -> tuple[float, float] | None:
    """Return the to-coordinates of the first BICYCLE leg, or None.

    The handoff point is where the cyclist leaves the bike — typically at a
    station for a bike-and-ride trip.  Returns None when there is no BICYCLE
    leg or when the leg's to-coordinates are not available.
    """
    for leg in itinerary.legs:
        if leg.mode == "BICYCLE":
            if leg.to_lat is not None and leg.to_lon is not None:
                return (leg.to_lat, leg.to_lon)
            return None
    return None
