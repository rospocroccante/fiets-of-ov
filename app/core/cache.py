"""A minimal async cache abstraction with a Redis and an in-memory implementation.

The interface is deliberately tiny — string `get`/`set` with a TTL — because that is all
the rain cache needs, and a small surface keeps the in-memory test double trivial. The
cache is treated as a *best-effort optimisation*, never a hard dependency: callers wrap
access so a Redis outage degrades to a cache miss rather than a failed request (see
`app/services/rain.py`).

Why strings, not objects: Redis stores bytes/strings, so callers serialise to JSON. The
TTL is the *retention* bound (how long a value may live); whether a value is still "fresh
enough" to use is the caller's decision, made from data inside the value, not from expiry.
"""

from typing import Protocol

import redis.asyncio as redis

from app.core.config import get_settings


class Cache(Protocol):
    """Async string cache with TTL. Implementations must not be a hard dependency."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class InMemoryCache:
    """Process-local cache for tests. TTL is accepted but not enforced — freshness is
    judged by the caller from the stored value, so unexpired-but-stale entries persist
    and remain available for the service's stale-fallback path."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._store[key] = value


class RedisCache:
    """Cache backed by Redis. Errors are intentionally *not* swallowed here — the caller
    decides the fail-open policy — but every value is written with an expiry so stale
    forecasts can't linger past their usefulness.

    A socket timeout is mandatory: the caller's fail-open only catches *raised* errors,
    so without a timeout a connected-but-wedged Redis would hang the request forever
    instead of failing open. The timeout turns a hang into a catchable error.
    """

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        timeout = timeout if timeout is not None else settings.redis_timeout_seconds
        self._redis = redis.from_url(
            url or settings.redis_url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
        )

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._redis.set(key, value, ex=ttl_seconds)
