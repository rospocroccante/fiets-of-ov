"""Async engine and session wiring for Postgres.

The engine and sessionmaker are process-wide singletons (created lazily, so importing this
module never opens a connection — the app boots even with the DB down). `get_session` is
the FastAPI dependency: one `AsyncSession` per request, closed automatically. Tests
override it to point at a test database.
"""

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Process-wide async engine. `pool_pre_ping` recycles connections dropped by Postgres."""
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False so returned ORM objects stay usable after the request commits.
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield a request-scoped AsyncSession, closing it when the request ends."""
    async with _sessionmaker()() as session:
        yield session
