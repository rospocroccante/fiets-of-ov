"""The `notifications` table: an outbox of rain alerts actually sent (Phase 6).

One row per alert delivered for a trip on a given departure *day*. The table is an
outbox: writing the row is the durable record of "we decided to warn this user about rain
on this trip today", and the pluggable `Notifier` then delivers it. That ordering is what
keeps delivery idempotent — the row is the source of truth, not the side effect.

The (trip_alert_id, departure_date) UNIQUE constraint is the idempotency guard: at most
one alert per trip per departure day. The notify service inserts with
`on_conflict_do_nothing` against exactly this constraint, so a re-run of the 5-minute
scheduler within the same day can never double-notify. `departure_date` is a plain DATE
(not a timestamp) precisely so the uniqueness is per *day*, independent of which scheduler
tick first fired.

Both foreign keys cascade on delete: removing a trip alert or a user drops the alerts we
recorded for them, since an outbox row is meaningless without its trip and owner.
`trip_alert_id` is indexed because lookups (and the FK itself) are by trip alert.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Notification(Base):
    """An outbox row recording one rain alert sent for a trip on a departure day."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Indexed FK: alerts are looked up by trip alert, and CASCADE drops them with the trip.
    trip_alert_id: Mapped[int] = mapped_column(
        ForeignKey("trip_alerts.id", ondelete="CASCADE"), index=True
    )
    # Denormalised owner so delivery/auditing never has to join through trip_alerts;
    # CASCADE drops the row when the account is deleted.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The advice we acted on, copied in so the outbox row is self-contained even if the
    # underlying forecast/trip later changes.
    recommendation: Mapped[str]
    reason: Mapped[str]
    # The departure *day* this alert covers — a DATE so idempotency is per day (see module
    # docstring); the unique constraint below keys on it.
    departure_date: Mapped[date] = mapped_column(Date)
    # Server-set send timestamp (now()), authoritative regardless of the worker's clock.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Idempotency guard: one alert per trip per departure day. The notify service's
        # insert(...).on_conflict_do_nothing targets this constraint by these columns, so
        # the name here is a hard contract — do not rename without updating that insert.
        UniqueConstraint(
            "trip_alert_id",
            "departure_date",
            name="uq_notifications_trip_alert_departure_date",
        ),
    )
