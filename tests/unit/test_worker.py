"""Smoke tests for the ARQ worker wiring (Phase 6).

The worker is a thin shell around `app.services.notify.run_due_checks`, whose logic is
covered by its own (DB-backed) tests. Here we only assert the wiring exists and is
import-safe: importing `app.workers.main` must not need Redis or a database, the cron
schedule must be non-empty, and the cron entrypoint must be an awaitable callable.

We deliberately do NOT run the ARQ event loop — that needs a live Redis — so these tests
stay fully offline.
"""

import inspect

from app.workers.main import WorkerSettings, check_trip_alerts


def test_worker_settings_has_cron_jobs() -> None:
    """WorkerSettings schedules at least one cron job for the due-check sweep."""
    assert WorkerSettings.cron_jobs
    assert len(WorkerSettings.cron_jobs) >= 1


def test_check_trip_alerts_is_a_coroutine_function() -> None:
    """The cron entrypoint is an async callable ARQ can await each tick."""
    assert callable(check_trip_alerts)
    assert inspect.iscoroutinefunction(check_trip_alerts)


def test_worker_wires_startup_hook() -> None:
    """on_startup is an awaitable hook.

    It exists to configure logging: arq only sets up its own `arq.*` loggers, so without
    the hook the LogNotifier's alert lines — the MVP delivery channel — would be dropped.
    The hook's behaviour is covered in test_logging_config.py; here we assert the wiring.
    """
    assert inspect.iscoroutinefunction(WorkerSettings.on_startup)


def test_cron_fires_once_every_five_minutes() -> None:
    """The cron runs on minutes 0,5,…,55 at second 0 — once per 5 min, not 60x/minute.

    arq defaults an unspecified `second` to 0; pinning it here guards against a future arq
    change (or an accidental second=None) silently turning the sweep into a per-second storm.
    """
    job = WorkerSettings.cron_jobs[0]
    assert job.minute == set(range(0, 60, 5))
    assert job.second == 0
