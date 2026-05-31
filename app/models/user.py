"""The `users` table: registered accounts that own trip alerts (Phase 5).

One row per account. `email` is the login identifier, so it carries a unique index — the
auth flow looks users up by email on every token request and must reject duplicates at
registration. We store only a bcrypt hash, never the plaintext password. `created_at` is
filled by the database (`server_default=now()`) so the timestamp is authoritative and
consistent regardless of the inserting client's clock.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """A registered account, identified by its email and authenticated by a bcrypt hash."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Unique + indexed: email is the login handle, looked up on every token request and
    # constrained to one account per address. The naming convention turns this into the
    # ix_users_email UNIQUE index that the migration creates by hand.
    email: Mapped[str] = mapped_column(unique=True, index=True)
    # bcrypt hash only — the plaintext never touches the database.
    hashed_password: Mapped[str]
    # Set by Postgres (now()), not the app, so the value is server-authoritative and the
    # column stays NOT NULL without the ORM having to supply it.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
