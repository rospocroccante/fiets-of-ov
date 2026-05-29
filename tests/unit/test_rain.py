"""Rain service: cache + Buienradar + stale fallback — the Phase 3 resilience layer.

This is where graceful degradation lives. The service serves a fresh cached forecast
without touching the network, refetches when the cache is stale, falls back to a stale
cached forecast when Buienradar is down, and returns None only when there is no data at
all. Crucially it is *fail-open*: a cache error must never break the request.

Buienradar is mocked with respx; the clock is injected so freshness is deterministic.
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from app.clients.buienradar import BuienradarClient
from app.core.cache import InMemoryCache
from app.services.rain import RainService

RAIN_URL = "https://rain.test/raintext"
RAINTEXT = "000|14:00\n109|14:05\n000|14:10\n"
LAT, LON = 52.37, 4.89
T0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


class _Clock:
    """Mutable injected clock so a single test can advance time between calls."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _service(cache, clock, buienradar=None) -> RainService:
    buienradar = buienradar or BuienradarClient(base_url=RAIN_URL)
    return RainService(buienradar, cache, fresh_seconds=300, retention_seconds=7200, now=clock)


@respx.mock
async def test_fresh_cache_hit_skips_network():
    route = respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAINTEXT))
    clock = _Clock(T0)
    service = _service(InMemoryCache(), clock)

    first = await service.get_forecast(lat=LAT, lon=LON)  # fetch + cache
    clock.now = T0 + timedelta(seconds=100)  # still within the 5-min fresh window
    second = await service.get_forecast(lat=LAT, lon=LON)  # served from cache

    assert route.calls.call_count == 1  # second call did NOT hit Buienradar
    assert [s.mm_per_h for s in second.slots] == [s.mm_per_h for s in first.slots]


@respx.mock
async def test_stale_cache_refetches_when_buienradar_ok():
    route = respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAINTEXT))
    clock = _Clock(T0)
    service = _service(InMemoryCache(), clock)

    await service.get_forecast(lat=LAT, lon=LON)
    clock.now = T0 + timedelta(seconds=600)  # past the fresh window
    await service.get_forecast(lat=LAT, lon=LON)

    assert route.calls.call_count == 2  # stale -> refetched fresh data


@respx.mock
async def test_buienradar_failure_serves_stale_cache():
    respx.get(RAIN_URL).mock(side_effect=[httpx.Response(200, text=RAINTEXT), httpx.Response(503)])
    clock = _Clock(T0)
    service = _service(InMemoryCache(), clock)

    await service.get_forecast(lat=LAT, lon=LON)  # primes the cache
    clock.now = T0 + timedelta(seconds=600)  # stale -> will try to refetch
    result = await service.get_forecast(lat=LAT, lon=LON)  # refetch 503s

    assert result is not None  # served the stale cached forecast, not None
    assert len(result.slots) == 3


@respx.mock
async def test_buienradar_failure_without_cache_returns_none():
    respx.get(RAIN_URL).mock(return_value=httpx.Response(503))
    service = _service(InMemoryCache(), _Clock(T0))

    assert await service.get_forecast(lat=LAT, lon=LON) is None


@respx.mock
async def test_cache_error_falls_back_to_network():
    # If Redis is down, the cache must behave like a miss, not break the request.
    class _BrokenCache:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ttl_seconds):
            raise ConnectionError("redis down")

    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAINTEXT))
    service = _service(_BrokenCache(), _Clock(T0))

    result = await service.get_forecast(lat=LAT, lon=LON)

    assert result is not None
    assert len(result.slots) == 3


KEY = f"rain:{LAT:.2f},{LON:.2f}"


@respx.mock
async def test_corrupt_cache_value_is_treated_as_miss():
    # A non-JSON cached value must not 500 the request: decode failure = cache miss,
    # so we fall through to a fresh Buienradar fetch. (json.loads must be inside the guard.)
    route = respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAINTEXT))
    cache = InMemoryCache()
    await cache.set(KEY, "not-json{{{", ttl_seconds=7200)
    service = _service(cache, _Clock(T0))

    result = await service.get_forecast(lat=LAT, lon=LON)

    assert result is not None
    assert len(result.slots) == 3
    assert route.calls.call_count == 1  # corrupt entry ignored, fetched fresh


@respx.mock
async def test_wrong_shape_cache_envelope_is_treated_as_miss():
    # Valid JSON but the wrong shape (e.g. after a schema change / partial write) must
    # also degrade to a miss, not raise KeyError and 500.
    route = respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text=RAINTEXT))
    cache = InMemoryCache()
    await cache.set(KEY, '{"unexpected": "shape"}', ttl_seconds=7200)
    service = _service(cache, _Clock(T0))

    result = await service.get_forecast(lat=LAT, lon=LON)

    assert result is not None
    assert route.calls.call_count == 1


@respx.mock
async def test_malformed_buienradar_text_degrades_instead_of_crashing():
    # Buienradar returns HTTP 200 but unparseable text: this raises ValueError in the
    # client, which is NOT an httpx error. The service must still degrade (return None),
    # not let the ValueError bubble into a 500.
    respx.get(RAIN_URL).mock(return_value=httpx.Response(200, text="totally not raintext"))
    service = _service(InMemoryCache(), _Clock(T0))

    assert await service.get_forecast(lat=LAT, lon=LON) is None
