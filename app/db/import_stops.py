"""Import GVB stops from a GTFS feed into the `stops` table.

Reads `stops.txt` out of a GTFS zip and **upserts** on `stop_id` (Postgres
`ON CONFLICT DO UPDATE`), so re-running simply refreshes the data — never duplicates it.
The import is defensive about messy feeds: a stop without coordinates or with unparseable
numbers is skipped (logged), and duplicate `stop_id`s within one feed are de-duplicated
(last wins) so a single bad/duplicate row can't abort the whole import.

Run it as a one-off after migrating:

    python -m app.db.import_stops [path-to-gtfs.zip]   # default: otp/data/gtfs-gvb.zip
"""

import asyncio
import csv
import io
import logging
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_engine
from app.models.stop import Stop

logger = logging.getLogger(__name__)

DEFAULT_GTFS = Path("otp/data/gtfs-gvb.zip")

# Columns required to place a stop; the import refuses a feed missing any of them.
_REQUIRED_COLUMNS = {"stop_id", "stop_name", "stop_lat", "stop_lon"}
# Columns refreshed on conflict — everything except the immutable primary key.
_UPDATABLE = (
    "code",
    "name",
    "lat",
    "lon",
    "location_type",
    "parent_station",
    "platform_code",
    "zone_id",
)


def _parse_row(row: dict) -> dict | None:
    """Turn a GTFS row into a Stop dict, or None if it can't be placed (skipped)."""
    lat, lon = (row.get("stop_lat") or "").strip(), (row.get("stop_lon") or "").strip()
    if not lat or not lon:
        return None  # unplaceable stop — skip rather than store NULL coordinates
    try:
        lat_f, lon_f = float(lat), float(lon)
        loc_raw = (row.get("location_type") or "").strip()
        location_type = int(loc_raw) if loc_raw else 0
    except ValueError:
        # A single malformed row must not abort the whole feed; drop it and move on.
        logger.warning(
            "skipping stop %r: unparseable coordinates/location_type", row.get("stop_id")
        )
        return None
    return {
        "stop_id": row["stop_id"],
        "code": row.get("stop_code") or None,
        "name": row["stop_name"],
        "lat": lat_f,
        "lon": lon_f,
        "location_type": location_type,
        "parent_station": row.get("parent_station") or None,
        "platform_code": row.get("platform_code") or None,
        "zone_id": row.get("zone_id") or None,
    }


def _read_stops(zip_path: Path) -> Iterator[dict]:
    """Yield one row dict per placeable stop in the GTFS feed's `stops.txt`."""
    with zipfile.ZipFile(zip_path) as zf:
        if "stops.txt" not in zf.namelist():
            raise ValueError(f"{zip_path} contains no stops.txt — is it a GTFS feed?")
        with zf.open("stops.txt") as raw:
            # utf-8-sig drops the BOM some GTFS publishers prepend to stops.txt.
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"stops.txt is missing required columns: {sorted(missing)}")
            for row in reader:
                if (parsed := _parse_row(row)) is not None:
                    yield parsed


async def import_stops(zip_path: Path, session: AsyncSession) -> int:
    """Upsert every placeable stop from `zip_path` into the DB; return the row count.

    Idempotent: a second run updates the same rows in place (conflict on `stop_id`).
    Duplicate `stop_id`s within the feed are collapsed (last wins) — a single
    `INSERT ... ON CONFLICT` statement may not touch the same conflict row twice.
    """
    # De-dup by stop_id; dict preserves insertion order, so the last occurrence wins.
    rows = list({row["stop_id"]: row for row in _read_stops(zip_path)}.values())
    if not rows:
        return 0
    stmt = insert(Stop).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["stop_id"],
        set_={col: stmt.excluded[col] for col in _UPDATABLE},
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)


async def _main() -> None:
    zip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GTFS
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        count = await import_stops(zip_path, session)
    print(f"imported {count} stops from {zip_path}")


if __name__ == "__main__":
    asyncio.run(_main())
