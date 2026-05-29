"""In-memory cache used as the test/double for the Redis cache.

The cache contract is intentionally tiny — string get/set with a TTL — so the rain
service can be tested without a real Redis. RedisCache itself is exercised in
integration, not here. The TTL is accepted but not enforced in-memory: forecast
freshness is decided by the service from a stored timestamp, not by cache expiry.
"""

from app.core.cache import InMemoryCache, RedisCache


async def test_set_then_get_round_trips():
    cache = InMemoryCache()
    await cache.set("k", "value", ttl_seconds=60)
    assert await cache.get("k") == "value"


async def test_get_missing_key_returns_none():
    assert await InMemoryCache().get("absent") is None


def test_rediscache_sets_socket_timeouts():
    # The cache is consulted on every request; without a socket timeout a wedged-but-
    # connected Redis would hang the request forever (errors are caught, hangs are not).
    cache = RedisCache(url="redis://localhost:6379/0", timeout=1.5)
    kwargs = cache._redis.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1.5
    assert kwargs["socket_connect_timeout"] == 1.5
