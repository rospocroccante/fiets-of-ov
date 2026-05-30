"""Alembic migration environment (async).

Runs migrations through an async SQLAlchemy engine. The URL comes from app settings (not
alembic.ini) so there is one source of truth and no credentials in git. Models are imported
so `Base.metadata` is fully populated for autogenerate.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.models import stop as _stop  # noqa: F401 — register the Stop table on the metadata
from app.models import trip_alert as _trip_alert  # noqa: F401 — register trip_alerts
from app.models import user as _user  # noqa: F401 — register the User table on the metadata

config = context.config
# Use a URL the caller already set (e.g. tests pointing at a throwaway DB); otherwise fall
# back to the app's DATABASE_URL. Either way the ini stays secret-free.
config.set_main_option(
    "sqlalchemy.url",
    config.get_main_option("sqlalchemy.url") or get_settings().database_url,
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a DB connection (alembic upgrade --sql)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Open an async connection and run migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
