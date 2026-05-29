"""Async client for Nominatim (OpenStreetMap) forward geocoding.

Turns a free-text place name ("Amsterdam Centraal", "Vondelpark") into `(lat, lon)` so
the advice endpoint can accept names instead of raw coordinates. OTP's own geocoder only
knows transit stops from the GTFS feed, so it can't resolve POIs like a park — Nominatim
covers arbitrary place names.

Quirks worth knowing (and why this wrapper exists):

- Nominatim's usage policy **requires a valid, identifying User-Agent** and asks for at
  most ~1 request/second. We send a configured User-Agent and cache results (place names
  don't move) so repeat lookups never re-hit the API.
- Results come back as a JSON list, best match first, with `lat`/`lon` as **strings**.
  An empty list means "no match" — we raise `GeocodeNotFound` (a caller input problem),
  distinct from a transport/HTTP failure, which propagates as `httpx.HTTPError` (an
  upstream problem). Callers map these to 400 vs 502 respectively.
- We bound the search to the Amsterdam bbox (`viewbox` + `bounded=1`, `countrycodes=nl`)
  so a bare "Centraal" resolves to Amsterdam Centraal, not a same-named place elsewhere.
"""

import httpx

from app.core.config import get_settings

# Amsterdam-only service: the search box is a domain constant, not config. Order is
# Nominatim's `viewbox` convention: lon_min,lat_min,lon_max,lat_max (two opposite corners).
AMSTERDAM_VIEWBOX = "4.728,52.278,5.079,52.431"


class GeocodeNotFound(Exception):
    """Raised when a place name matches no location within the Amsterdam bounds."""


class GeocoderClient:
    """Resolves Amsterdam place names to coordinates via the Nominatim search API."""

    def __init__(
        self,
        base_url: str | None = None,
        user_agent: str | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = base_url or settings.nominatim_url
        self._user_agent = user_agent or settings.geocoder_user_agent
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds
        # Place names are stable, so an in-process cache (keyed by normalised query) both
        # speeds up repeats and keeps us within Nominatim's ~1 req/s usage policy.
        self._cache: dict[str, tuple[float, float]] = {}

    async def geocode(self, query: str) -> tuple[float, float]:
        """Return `(lat, lon)` for `query`, bounded to Amsterdam.

        Raises `GeocodeNotFound` if nothing matches, or `httpx.HTTPError` if the request
        fails or returns a non-2xx status. Repeat lookups are served from the cache.
        """
        key = query.strip().lower()
        if key in self._cache:
            return self._cache[key]

        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "nl",
            "viewbox": AMSTERDAM_VIEWBOX,
            "bounded": 1,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                self._base_url, params=params, headers={"User-Agent": self._user_agent}
            )
            response.raise_for_status()
            results = response.json()

        if not results:
            raise GeocodeNotFound(f"no Amsterdam location found for {query!r}")

        top = results[0]
        coords = (float(top["lat"]), float(top["lon"]))
        self._cache[key] = coords
        return coords
