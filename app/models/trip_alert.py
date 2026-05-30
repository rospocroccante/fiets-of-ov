"""The `trip_alerts` table: a user's saved A→B trips to be warned about rain (Phase 5).

One row per saved trip. Each carries pre-resolved origin/destination coordinates so the
notification worker never has to geocode again — the place names are resolved once at
create time. `days` holds the ISO weekdays (1=Mon … 7=Sun) the trip recurs on, stored as a
SmallInteger array (a Postgres-native ARRAY, not a join table) because the set is tiny,
fixed-size, and only ever read/written wholesale. `user_id` cascades on delete so removing
an account drops its trip alerts with it. It is indexed because the list endpoint and the
worker both filter by owner.
"""

from datetime import datetime, time

from sqlalchemy import ARRAY, DateTime, ForeignKey, SmallInteger, Time, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TripAlert(Base):
    """A user's saved trip: origin, destination, departure time, and recurrence days."""

    __tablename__ = "trip_alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Indexed FK: every query for a user's trip alerts filters on this, and ondelete
    # CASCADE means deleting a user removes their trip alerts in the database itself.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str]

    # Origin/destination are stored both as the human label and as the coordinates resolved
    # at create time, so the notification worker plans routes without re-geocoding.
    origin: Mapped[str]
    origin_lat: Mapped[float]
    origin_lon: Mapped[float]
    destination: Mapped[str]
    dest_lat: Mapped[float]
    dest_lon: Mapped[float]

    # Local clock time the trip departs (no date/zone — recurrence is what `days` encodes).
    departure_time: Mapped[time] = mapped_column(Time)
    # ISO weekdays (1=Mon … 7=Sun) the trip recurs on. A Postgres SmallInteger ARRAY: the
    # set is small and always handled as a whole, so a join table would be overkill.
    days: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))

    # Server-set creation timestamp (see User.created_at for the rationale).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
