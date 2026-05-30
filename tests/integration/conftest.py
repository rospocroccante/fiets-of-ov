"""Integration-test harness: a real Postgres, migrated, with per-test isolation.

These tests need Postgres (the nearby-stop query is plain SQL but exercises the real
engine and the migrated schema). If no test DB is reachable the whole module **skips**,
so the offline unit suite stays green without infrastructure. Point at a throwaway DB via
`TEST_DATABASE_URL`; the default matches the docker-compose Postgres.

The schema is created by running the actual Alembic migration (not metadata.create_all),
so these tests also prove the migration applies cleanly. Each test runs inside an outer
transaction that is rolled back at the end — so tests are isolated AND never persist to
(or TRUNCATE) the target database, which may be shared.
"""

import asyncio
import os

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://fiets:fiets@localhost:5432/fiets_test"
)


async def _can_connect(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect():
            return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def _migrated_db():
    """Migrate the test DB once. Skip the module only if the DB is unreachable — a real
    migration failure must surface, not be hidden as a skip, so connectivity is probed
    separately from running the migration."""
    if not asyncio.run(_can_connect(TEST_DATABASE_URL)):
        pytest.skip(f"no reachable test Postgres at {TEST_DATABASE_URL}")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")  # a failure here is a real bug, not a skip
    yield


@pytest_asyncio.fixture
async def session(_migrated_db):
    """An isolated AsyncSession per test.

    The session runs on a connection inside an outer transaction we roll back when the
    test ends; `join_transaction_mode="create_savepoint"` turns the test's own commits
    into savepoints within it. Nothing the test writes is ever persisted, so concurrent
    or repeated runs can't clobber each other or wipe a shared database.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    connection = await engine.connect()
    transaction = await connection.begin()
    # Clean slate *inside* the rolled-back transaction: each test sees an empty table
    # regardless of what the target DB already holds, and the deletion is undone on
    # rollback — so a populated, possibly shared DB is left exactly as it was.
    await connection.execute(text("DELETE FROM stops"))
    maker = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session = maker()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
