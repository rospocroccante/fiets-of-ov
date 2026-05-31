"""create notifications table

Revision ID: 0003_notifications
Revises: 0002_users_trip_alerts
Create Date: 2026-05-31

Phase 6: the `notifications` outbox — one row per rain alert actually sent for a trip on a
given departure day. Hand-written so it needs no live DB to author and its constraint/index
names match the model naming convention exactly (pk_notifications,
fk_notifications_trip_alert_id_trip_alerts CASCADE, fk_notifications_user_id_users CASCADE,
ix_notifications_trip_alert_id, and the uq_notifications_trip_alert_departure_date UNIQUE
that the notify service's on-conflict insert targets). `departure_date` is a DATE and
`created_at` carries server_default now() so `alembic check` (compare_type +
compare_server_default) stays clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_notifications"
down_revision: str | None = "0002_users_trip_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trip_alert_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("recommendation", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        # DATE (not timestamp): idempotency keys on the departure *day*.
        sa.Column("departure_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # CASCADE so deleting a trip alert drops the alerts recorded for it.
        sa.ForeignKeyConstraint(
            ["trip_alert_id"],
            ["trip_alerts.id"],
            name="fk_notifications_trip_alert_id_trip_alerts",
            ondelete="CASCADE",
        ),
        # CASCADE so deleting an account drops the alerts recorded for it.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notifications_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        # Idempotency guard: at most one alert per trip per departure day. The notify
        # service inserts with on_conflict_do_nothing against exactly this constraint.
        sa.UniqueConstraint(
            "trip_alert_id",
            "departure_date",
            name="uq_notifications_trip_alert_departure_date",
        ),
    )
    # Non-unique index: notifications are looked up by trip alert (and by the FK).
    op.create_index("ix_notifications_trip_alert_id", "notifications", ["trip_alert_id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_trip_alert_id", table_name="notifications")
    op.drop_table("notifications")
