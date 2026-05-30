"""create users and trip_alerts tables

Revision ID: 0002_users_trip_alerts
Revises: 0001_create_stops
Create Date: 2026-05-30

Phase 5: accounts (`users`) and their saved trips (`trip_alerts`). Hand-written so it
needs no live DB to author and its constraint/index names match the model naming
convention (pk_users, ix_users_email UNIQUE, pk_trip_alerts, fk_trip_alerts_user_id_users,
ix_trip_alerts_user_id). Column types deliberately mirror the models — Float coordinates,
Time departure, a Postgres SmallInteger ARRAY for `days`, and `server_default=now()` on
`created_at` — so `alembic check` (compare_type + compare_server_default) stays clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_users_trip_alerts"
down_revision: str | None = "0001_create_stops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    # UNIQUE index (not a plain index) — email is the login handle; one account per address.
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "trip_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("origin_lat", sa.Float(), nullable=False),
        sa.Column("origin_lon", sa.Float(), nullable=False),
        sa.Column("destination", sa.String(), nullable=False),
        sa.Column("dest_lat", sa.Float(), nullable=False),
        sa.Column("dest_lon", sa.Float(), nullable=False),
        sa.Column("departure_time", sa.Time(), nullable=False),
        # Postgres-native SmallInteger array, matching the model's ARRAY(SmallInteger).
        sa.Column("days", postgresql.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # CASCADE so deleting an account drops its trip alerts in the database itself.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_trip_alerts_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trip_alerts"),
    )
    # Non-unique index: the list endpoint and the worker both filter by owner.
    op.create_index("ix_trip_alerts_user_id", "trip_alerts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_trip_alerts_user_id", table_name="trip_alerts")
    op.drop_table("trip_alerts")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
