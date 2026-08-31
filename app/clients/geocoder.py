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
  upstream problem). A 2xx body that isn't the documented JSON shape raises
  `GeocoderError` (also an upstream problem). Callers map these to 400 vs 502.
- We bound the search to the Amsterdam bbox (`viewbox` + `bounded=1`, `countrycodes=nl`)
  so a bare "Centraal" resolves to Amsterdam Centraal, not a same-named place elsewhere.
"""

import httpx

from app.core.config import get_settings

# Amsterdam-only service: the search box is a domain constant, not config. Order is
# Nominatim's `viewbox` convention: lon_min,lat_min,lon_max,lat_max (two opposite corners).
AMSTERDAM_VIEWBOX = "4.728,52.278,5.079,52.431"

# Bound on the in-process name cache: unbounded growth is a slow leak in a long-lived
# process, and ~512 distinct place names comfortably covers a day of Amsterdam lookups.
_CACHE_MAX_ENTRIES = 512


class GeocodeNotFound(Exception):
    """Raised when a place name matches no location within the Amsterdam bounds."""


class GeocoderError(Exception):
    """Raised when Nominatim responds 2xx but the payload isn't the shape it documents.

    Distinct from `httpx.HTTPError` (transport / non-2xx) only in origin; both are
    upstream faults and callers map both to 502. Without this wrapper a proxy's HTML
    error page or a schema change would surface as a bare `ValueError`/`KeyError` — a
    500 that blames us for Nominatim's garbage."""


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
        # One shared AsyncClient, created lazily on first use so instantiation needs no
        # event loop; per-request construction would rebuild the SSL context every call
        # and never reuse a connection.
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        """The shared HTTP client, created on first use."""
        if self._client is None:
            # The identifying User-Agent Nominatim's policy requires is fixed for the
            # client's lifetime, so it lives on the shared client, not per request.
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the shared HTTP client and its pooled connections, if one was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def geocode(self, query: str) -> tuple[float, float]:
        """Return `(lat, lon)` for `query`, bounded to Amsterdam.

        Raises `GeocodeNotFound` if nothing matches, `httpx.HTTPError` if the request
        fails or returns a non-2xx status, or `GeocoderError` if a 2xx payload is not
        the JSON shape Nominatim documents. Repeat lookups are served from the cache.
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
        response = await self._http().get(self._base_url, params=params)
        response.raise_for_status()
        try:
            results = response.json()
        except ValueError as exc:
            raise GeocoderError(f"Nominatim returned non-JSON: {exc}") from exc

        if not results:
            raise GeocodeNotFound(f"no Amsterdam location found for {query!r}")

        try:
            top = results[0]
            coords = (float(top["lat"]), float(top["lon"]))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeocoderError(f"Nominatim returned a malformed result: {exc}") from exc
        self._cache[key] = coords
        if len(self._cache) > _CACHE_MAX_ENTRIES:
            # Evict the oldest inserted entry (dicts keep insertion order) so the cache
            # stays bounded without pulling in an LRU dependency.
            self._cache.pop(next(iter(self._cache)))
        return coords
