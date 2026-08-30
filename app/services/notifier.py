"""Notifier: the pluggable delivery layer for rain alerts.

The durable record of a sent alert is the `notifications` outbox row — written
transactionally and guarded by a unique (trip_alert_id, departure_date) constraint so an
alert is sent at most once per trip per departure day. The Notifier is only the *delivery*
side: given an already-persisted notification, push it to the user.

For the MVP that delivery is a log line (LogNotifier) — no external dependency, no network,
nothing that can fail. Real channels (push, email) plug in later by implementing the same
Protocol, so the notify service can stay agnostic about *how* an alert reaches the user, and
a fallible one is now safe to wire: the notify service stamps `notifications.delivered_at`
only on a successful send and re-attempts undelivered rows on each worker tick, so a
transient outage self-heals instead of leaving a row claiming an alert that never arrived.

That makes delivery at-least-once, which is the contract implementations should assume: a
`send()` that raises will be called again for the same notification on a later tick, so a
channel with its own idempotency key should derive it from `trip_alert_id` + the departure
day rather than assume one call per alert.

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
