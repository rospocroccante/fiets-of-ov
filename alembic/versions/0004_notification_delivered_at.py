"""add notifications.delivered_at

Revision ID: 0004_notification_delivered_at
Revises: 0003_notifications
Create Date: 2026-08-01

Turns the `notifications` table into a true outbox: the row is committed before delivery is
attempted, and `delivered_at` is stamped only once a send actually succeeds. A NULL means
"recorded but not delivered", which the worker retries on its next tick.

Nullable with no server default, on purpose — an existing row was written under the old
best-effort, single-attempt regime, so we genuinely do not know whether it reached anyone.
Backfilling now() would assert a delivery we cannot evidence; leaving them NULL is honest.
Those rows are not resurrected either: redelivery only considers alerts whose
`departure_date` is still current, and a rain warning for a past trip is worse than useless.

Hand-written like the migrations before it, so it needs no live DB to author and stays
consistent with the model (`alembic check` with compare_type/compare_server_default clean).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_notification_delivered_at"
down_revision: str | None = "0003_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "delivered_at")
