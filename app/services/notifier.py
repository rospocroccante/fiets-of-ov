"""Notifier: the pluggable delivery layer for rain alerts.

The durable record of a sent alert is the `notifications` outbox row — written
transactionally and guarded by a unique (trip_alert_id, departure_date) constraint so an
alert is sent at most once per trip per departure day. The Notifier is only the *delivery*
side: given an already-persisted notification, push it to the user.

For the MVP that delivery is a log line (LogNotifier) — no external dependency, no network,
nothing that can fail and leave the outbox row lying about a delivery that never happened.
Real channels (push, email) plug in later by implementing the same Protocol, so the notify
service can stay agnostic about *how* an alert reaches the user. A *fallible* channel needs
one more thing first: the notify service currently delivers best-effort, single-attempt
(failures are logged, not retried), so before wiring email/push add a `delivered_at` column
and per-tick re-delivery of undelivered rows (see Decisions) — otherwise a transient outage
would record an alert as sent that never arrived.

The Protocol is duck-typed on purpose: callers pass the SQLAlchemy `Notification`, but
anything exposing `trip_alert_id` and `reason` works, which keeps these notifiers trivially
unit-testable without a database.
"""

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """A delivery channel for a sent rain alert.

    Implementations take a persisted notification and push it to the user. They must not
    mutate or persist anything — the outbox row is already the source of truth — and should
    be safe to call exactly once per created notification.
    """

    async def send(self, notification: "NotificationLike") -> None:
        """Deliver the alert. Returns None; raise only on a genuine delivery failure."""
        ...


class NotificationLike(Protocol):
    """Structural shape a notifier reads: the trip-alert id and the human-readable reason.

    Declared separately so notifiers don't have to import the SQLAlchemy model (and so tests
    can pass a SimpleNamespace).
    """

    trip_alert_id: int
    reason: str


class LogNotifier:
    """MVP delivery: emit the alert as a structured log line.

    The outbox row is the real record; this just makes the send observable in logs. We log
    the trip_alert id and the reason so an operator can trace which alert fired and why,
    without leaking the full notification object.
    """

    async def send(self, notification: NotificationLike) -> None:
        """Log the rain alert for `notification`. Always succeeds (logging only)."""
        logger.info(
            "rain alert: trip_alert_id=%s reason=%s",
            notification.trip_alert_id,
            notification.reason,
        )
