"""Unit tests for the notifier delivery layer.

LogNotifier is the MVP delivery: the durable record is the outbox `notifications` row;
LogNotifier just emits a log line. These tests stay fully offline and duck-type the
notification (a SimpleNamespace exposing `trip_alert_id` and `reason` is enough), so they
don't depend on the SQLAlchemy model or a database.
"""

import logging
from types import SimpleNamespace

import pytest

from app.services.notifier import LogNotifier, Notifier


async def test_log_notifier_send_runs_on_duck_typed_object() -> None:
    """send() succeeds against any object exposing trip_alert_id + reason."""
    notifier = LogNotifier()
    notification = SimpleNamespace(trip_alert_id=42, reason="rain in 15 min → tram 13")

    # Must complete without raising; the return is None by contract.
    assert await notifier.send(notification) is None


def test_log_notifier_satisfies_notifier_protocol() -> None:
    """LogNotifier is a structural match for the Notifier Protocol."""
    assert isinstance(LogNotifier(), Notifier)


async def test_log_notifier_logs_trip_alert_id_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log line carries the trip_alert id and the reason for traceability."""
    notifier = LogNotifier()
    notification = SimpleNamespace(trip_alert_id=7, reason="rain in 10 min")

    with caplog.at_level(logging.INFO):
        await notifier.send(notification)

    assert "7" in caplog.text
    assert "rain in 10 min" in caplog.text
