"""The `stops` table: GVB stops imported from the GTFS feed.

One row per GTFS stop, keyed by the feed's own `stop_id`. `lat`/`lon` carry a composite
btree index because the nearby-stop query pre-filters on a bounding box (`lat`/`lon
BETWEEN …`) before ranking by exact distance — see `app/services/stops.py` and
`app/services/geo.py`. We keep `location_type`/`parent_station` so platforms can be told
apart from their parent stations later without re-importing.
"""

from sqlalchemy import Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Stop(Base):
    """A single GVB stop (GTFS `stops.txt` row) with its coordinates."""

    __tablename__ = "stops"

    # GTFS stop_id, e.g. "stoparea:513789" — stable across imports, so it's the PK and the
    # upsert conflict target.
    stop_id: Mapped[str] = mapped_column(primary_key=True)
    code: Mapped[str | None]
    name: Mapped[str]
    lat: Mapped[float]
    lon: Mapped[float]
    # GTFS location_type (0/empty = stop/platform, 1 = station, …); default 0 per the spec.
    # server_default matches the migration so the DB also fills it (and alembic check stays
    # clean when comparing server defaults).
    location_type: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    parent_station: Mapped[str | None]
    platform_code: Mapped[str | None]
    zone_id: Mapped[str | None]

    # Composite index serving the bounding-box prefilter of the nearby-stop search.
    __table_args__ = (Index("ix_stops_lat_lon", "lat", "lon"),)
