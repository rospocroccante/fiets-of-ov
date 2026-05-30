"""Resolving a trip endpoint (a `from`/`to` value) to coordinates.

A trip endpoint arrives as either explicit `"lat,lon"` coordinates or a free-text place
name. This service centralises the "parse-or-geocode" decision so every caller (the advice
endpoint, trip-alert creation) resolves places the same way, instead of duplicating the
branch inline.

Deliberately HTTP-agnostic: it raises the *domain* errors (`GeocodeNotFound` for an
unresolvable name, `httpx.HTTPError` for a geocoder outage) and lets the calling endpoint
map them to status codes (400 vs 502). Keeping the mapping out of here means the same
function is reusable from non-HTTP contexts (e.g. background workers) too.
"""

import math

from app.clients.geocoder import GeocoderClient


def _try_parse_latlon(value: str) -> tuple[float, float] | None:
    """Parse a `"lat,lon"` value into a valid coordinate pair, or None otherwise.

    Returns None for anything that isn't two finite, in-range numbers — so junk like
    "nan,inf" or "999,999" falls through to geocoding (where it fails cleanly as an
    unknown place) instead of being stored as a corrupt point the router would choke on.
    """
    try:
        lat_str, lon_str = value.split(",")
        lat, lon = float(lat_str), float(lon_str)
    except ValueError:
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


async def resolve_place(value: str, geocoder: GeocoderClient) -> tuple[float, float]:
    """Resolve a `from`/`to` value to `(lat, lon)`.

    Args:
        value: Either explicit `"lat,lon"` coordinates or a place name.
        geocoder: Client used to geocode a name (only called for names, never for
            coordinates).

    Returns:
        The `(lat, lon)` coordinate pair.

    Raises:
        GeocodeNotFound: The value is a name that matches no Amsterdam location — a
            caller input problem (callers map to HTTP 400).
        httpx.HTTPError: The geocoder upstream failed or returned a non-2xx — an upstream
            problem (callers map to HTTP 502).

    Coordinates are resolved locally with no network call; only a name hits Nominatim.
    """
    coords = _try_parse_latlon(value)
    if coords is not None:
        return coords
    # Not coordinates -> treat as a place name. We deliberately let GeocodeNotFound and
    # httpx.HTTPError propagate; the caller owns the HTTP status mapping.
    return await geocoder.geocode(value)
