"""Rain-forecast service: the caching + graceful-degradation layer over Buienradar.

The Buienradar client is a dumb adapter (fetch-or-raise). This service adds the Phase 3
resilience around it so the advice endpoint can stay simple and never crash on a flaky
upstream:

- **Cache as optimisation.** A forecast newer than `fresh_seconds` (~5 min, matching
  Buienradar's own cadence) is served straight from cache — no network call.
- **Stale-fallback as resilience.** Past the fresh window we refetch; if Buienradar is
  down, we serve the last cached forecast anyway (rain barely moves in a few minutes — a
  slightly old forecast beats none). The cache retains values for `retention_seconds`
  (~2h), after which the forecast horizon is exhausted and the entry expires.
- **Fail-open cache.** Every cache access is guarded: a Redis outage degrades to a miss,
  never a failed request. The cache is an optimisation, not a dependency.
- **Honest "no data".** Only when there is no forecast at all (Buienradar down *and* no
  usable cache) do we return None, which the decision engine renders as a degraded,
  bike-first answer. That None is the *only* degradation signal this service emits, and it
  is load-bearing: `recommend()` turns it into the `forecast_degraded` flag the API returns,
  so clients can tell "we checked, it's dry" from "we couldn't check". A stale-cache hit is
  deliberately not degraded — it is real data, just a few minutes old.

The clock is injectable so freshness is deterministic in tests.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from app.clients.buienradar import BuienradarClient, RainForecast
from app.core.cache import Cache

logger = logging.getLogger(__name__)


class RainService:
    """Fetches rain forecasts through a cache, degrading gracefully on upstream failure."""

    def __init__(
        self,
        buienradar: BuienradarClient,
        cache: Cache,
        *,
        fresh_seconds: int,
        retention_seconds: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._buienradar = buienradar
        self._cache = cache
        self._fresh_seconds = fresh_seconds
        self._retention_seconds = retention_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def get_forecast(self, *, lat: float, lon: float) -> RainForecast | None:
        """Return the rain forecast for `(lat, lon)`, or None if no data is available.

        Serves a fresh cache hit without any network call; otherwise refetches, falling
        back to a stale cached forecast if Buienradar is unavailable.
        """
        # Buienradar rounds coordinates anyway, so a 2-decimal key (~1 km) groups nearby
        # requests onto one cache entry — more hits, well within the forecast's resolution.
        key = f"rain:{lat:.2f},{lon:.2f}"
        cached = await self._read_cache(key)  # (forecast, age_seconds) or None

        if cached is not None and cached[1] < self._fresh_seconds:
            return cached[0]

        try:
            forecast = await self._buienradar.get_rain_forecast(lat=lat, lon=lon)
        except (httpx.HTTPError, ValueError):
            # httpx.* covers outages/timeouts/non-2xx; ValueError covers an upstream 200
            # whose body doesn't parse as raintext. Either way the live data is unusable,
            # so fall back to the stale cache, or degrade if there is none.
            if cached is not None:
                logger.warning("Buienradar unusable; serving stale cached forecast")
                return cached[0]
            logger.warning("Buienradar unusable and no cached forecast; degrading")
            return None

        await self._write_cache(key, forecast)
        return forecast

    # --- cache access (fail-open) ------------------------------------------------------

    async def _read_cache(self, key: str) -> tuple[RainForecast, float] | None:
        """Return `(forecast, age_seconds)` from cache, or None on miss/outage/poison.

        Every step — the cache read, JSON decode, schema validation and age computation —
        is inside one guard, so a Redis outage, a corrupt value, or a wrong-shape envelope
        (e.g. left over after a schema change) all degrade to a cache miss, never an
        exception that could 500 the request.
        """
        try:
            raw = await self._cache.get(key)
            if not raw:
                return None
            envelope = json.loads(raw)
            forecast = RainForecast.model_validate(envelope["forecast"])
            age = (self._now() - datetime.fromisoformat(envelope["fetched_at"])).total_seconds()
            return forecast, age
        except Exception:
            # Fail open: an unusable cache must behave like a miss, not break the request.
            logger.warning("cache read failed; treating as miss", exc_info=True)
            return None

    async def _write_cache(self, key: str, forecast: RainForecast) -> None:
        """Store the forecast with the fetch time, ignoring any cache error."""
        envelope = json.dumps(
            {"fetched_at": self._now().isoformat(), "forecast": forecast.model_dump(mode="json")}
        )
        try:
            await self._cache.set(key, envelope, ttl_seconds=self._retention_seconds)
        except Exception:
            logger.warning("cache set failed; continuing without caching", exc_info=True)
