"""ARQ worker: the scheduled shell around the rain-notification core.

This is the only part of Phase 6 that talks to the outside world (Redis, the DB, the OTP
and rain clients). All decision logic lives in `app.services.notify`; the worker just
wakes on a cron tick, assembles the dependencies, and hands them to `run_due_checks`.

Run it with:  arq app.workers.main.WorkerSettings

Kept deliberately import-safe: importing this module must not open a Redis connection, a DB
connection, or any socket — `RedisSettings.from_dsn` only parses the DSN, and the engine /
clients are constructed lazily inside `check_trip_alerts`. That lets a unit test import
`WorkerSettings` and the entrypoint without a live Redis (see tests/unit/test_worker.py).
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_buienradar_client, get_otp_client, get_rain_service
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine
from app.services.notifier import LogNotifier
from app.services.notify import run_due_checks

# Amsterdam-only service: the worker's wall clock is the same domain constant the notify
# core reasons in, so a tick's `now` lines up with the alerts' naive local departure times.
AMS = ZoneInfo("Europe/Amsterdam")


async def check_trip_alerts(ctx: dict[str, Any]) -> None:
    """ARQ cron entrypoint: run one due-check sweep over all trip alerts.

    Invoked on each cron tick (see `WorkerSettings.cron_jobs`). It builds a fresh DB session
    plus the OTP and rain clients, stamps `now` in Amsterdam time, and delegates the whole
    decide-and-notify flow to `run_due_checks`, which records and delivers at most one alert
    per trip per departure day (idempotency is enforced in the outbox, not here).

    Args:
        ctx: the ARQ job context (unused; the dependencies are constructed here so the
            worker has no startup wiring to maintain).

    Side effects:
        Opens a DB session and may INSERT into `notifications` and emit log lines via the
        notifier. Returns None — ARQ ignores cron job return values.
    """
    settings = get_settings()
    otp = get_otp_client()
    rain_service = get_rain_service()
    notifier = LogNotifier()
    now = datetime.now(AMS)

    # A per-tick session, not the request-scoped one: there is no FastAPI request here.
    # Built from the shared singleton engine so we reuse its pool across ticks.
    sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sessionmaker() as session:
        await run_due_checks(
            session,
            now=now,
            otp=otp,
            rain_service=rain_service,
            notifier=notifier,
            lead_minutes=settings.notify_lead_minutes,
        )


async def startup(ctx: dict[str, Any]) -> None:
    """ARQ startup hook: make the app's own loggers visible.

    arq only configures its `arq.*` loggers, so without this the worker process has no
    root handler and every `app.*` log line is dropped — including LogNotifier's rain
    alerts, which are the MVP's entire delivery channel. `configure_logging` is a
    guarded basicConfig, so a deployment that pre-configures logging is left alone.
    """
    configure_logging()


async def shutdown(ctx: dict[str, Any]) -> None:
    """ARQ shutdown hook: close the shared HTTP clients' pooled connections.

    The worker reuses the process-wide client singletons across ticks (see
    `check_trip_alerts`); this releases their sockets when the worker exits. `aclose()`
    on a never-used client is a no-op, so this is safe even if no tick ever ran.
    """
    await get_otp_client().aclose()
    await get_buienradar_client().aclose()


# arq cron is minute-granular, so the seconds-based NOTIFY_SCHEDULER_SECONDS maps to a
# minute step: sub-60 s values clamp to every minute (the finest cadence arq can run),
# and the 300 s default fires on minutes 0,5,10,…,55, aligned to the wall clock.
_SWEEP_STEP_MINUTES = max(1, get_settings().notify_scheduler_seconds // 60)


class WorkerSettings:
    """ARQ worker configuration (the object `arq` is pointed at to run the worker).

    `redis_settings` is parsed from the DSN at import time (no connection is opened); the
    cron sweep cadence comes from `notify_scheduler_seconds` (default 300 s = every 5 min,
    matching Buienradar's ~5-min update cadence, so a tick never re-evaluates against
    unchanged forecast data), translated to the minute-set arq understands.
    """

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    cron_jobs = [cron(check_trip_alerts, minute=set(range(0, 60, _SWEEP_STEP_MINUTES)))]
    on_startup = startup
    on_shutdown = shutdown
