"""The `Notification` model: outbox row + idempotency constraint (Phase 6, Foundation A).

These checks run fully offline against `Notification.__table__` — no database. They pin
the two things the rest of Phase 6 relies on: that the model imports and maps cleanly,
and that the (trip_alert_id, departure_date) UNIQUE constraint exists under the exact name
the on-conflict insert in `services/notify.py` will target. If either drifts, the
idempotency guarantee (at most one alert per trip per departure day) silently breaks.
"""

from sqlalchemy import Date, UniqueConstraint

from app.models.notification import Notification


def test_notification_maps_to_notifications_table() -> None:
    """The model registers as the `notifications` table with the expected columns."""
    table = Notification.__table__
    assert table.name == "notifications"
    # Columns the outbox row and the worker's insert/select both depend on.
    expected = {
        "id",
        "trip_alert_id",
        "user_id",
        "recommendation",
        "reason",
        "departure_date",
        "created_at",
    }
    assert expected <= set(table.columns.keys())


def test_departure_date_is_a_date_column() -> None:
    """`departure_date` is a plain DATE — idempotency is per departure *day*, not instant."""
    assert isinstance(Notification.__table__.c.departure_date.type, Date)


def test_unique_constraint_name_matches_on_conflict_target() -> None:
    """The (trip_alert_id, departure_date) UNIQUE constraint carries the exact pinned name.

    `services/notify.py` runs an on-conflict-do-nothing insert against these columns; the
    constraint name is the durable contract that makes at-most-one-alert-per-trip-per-day
    hold. A rename here would silently disable the idempotency guard.
    """
    uniques = [c for c in Notification.__table__.constraints if isinstance(c, UniqueConstraint)]
    assert any(
        u.name == "uq_notifications_trip_alert_departure_date"
        and [col.name for col in u.columns] == ["trip_alert_id", "departure_date"]
        for u in uniques
    )


def test_trip_alert_id_is_indexed() -> None:
    """`trip_alert_id` is indexed: the worker looks notifications up by trip alert."""
    indexed_columns = {col.name for ix in Notification.__table__.indexes for col in ix.columns}
    assert "trip_alert_id" in indexed_columns
