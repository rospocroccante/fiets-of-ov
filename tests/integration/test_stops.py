"""Integration tests for the GVB stops feature against a real, migrated Postgres.

Covers the import upsert (idempotent, skips unplaceable rows), the nearby-stop query
(bounding-box prefilter + distance ranking), and the `/v1/stops` endpoint end-to-end.
"""

import zipfile

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.import_stops import import_stops
from app.db.session import get_session
from app.main import app
from app.models.stop import Stop
from app.services.stops import nearby_stops

_HEADER = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,"
    "parent_station,stop_timezone,wheelchair_boarding,platform_code,zone_id"
)

# Amsterdam Centraal, the query origin for the nearby tests.
CS_LAT, CS_LON = 52.3791, 4.9003


def _make_gtfs(tmp_path, rows: list[str]):
    """Write a minimal GTFS zip containing just stops.txt."""
    path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("stops.txt", "\n".join([_HEADER, *rows]) + "\n")
    return path


async def test_import_skips_unplaceable_rows_and_is_idempotent(tmp_path, session):
    gtfs = _make_gtfs(
        tmp_path,
        [
            "s1,C1,Centraal,52.3791,4.9003,1,,,0,,",
            "s2,C2,Dam,52.3731,4.8926,1,,,0,,",
            "s3,C3,No Coords,,,1,,,0,,",  # missing lat/lon -> skipped
        ],
    )

    count = await import_stops(gtfs, session)

    assert count == 2  # the coordinate-less row was skipped
    total = await session.scalar(select(func.count()).select_from(Stop))
    assert total == 2

    # Re-import with a renamed stop: upsert must update in place, not duplicate.
    gtfs2 = _make_gtfs(tmp_path, ["s1,C1,Centraal Station,52.3791,4.9003,1,,,0,,"])
    await import_stops(gtfs2, session)

    assert await session.scalar(select(func.count()).select_from(Stop)) == 2
    renamed = await session.get(Stop, "s1")
    assert renamed.name == "Centraal Station"


async def test_import_dedups_duplicate_stop_id(tmp_path, session):
    # Two rows share a stop_id: a single INSERT ... ON CONFLICT can't touch the same row
    # twice, so the import must de-dup (last wins) rather than crash the whole batch.
    gtfs = _make_gtfs(
        tmp_path,
        [
            "dup,C,First,52.3791,4.9003,1,,,0,,",
            "dup,C,Second,52.3731,4.8926,1,,,0,,",
        ],
    )

    count = await import_stops(gtfs, session)

    assert count == 1
    assert (await session.get(Stop, "dup")).name == "Second"


async def test_import_skips_malformed_rows(tmp_path, session):
    # A bad coordinate or location_type in one row must not abort the whole feed.
    gtfs = _make_gtfs(
        tmp_path,
        [
            "ok,C,Good,52.3791,4.9003,1,,,0,,",
            "bad1,C,BadLat,abc,4.9,1,,,0,,",
            "bad2,C,BadLoc,52.37,4.90,X,,,0,,",
            "bad3,C,WsCoord, , ,1,,,0,,",
        ],
    )

    count = await import_stops(gtfs, session)

    assert count == 1  # only the good row survived
    assert await session.get(Stop, "ok") is not None


async def test_nearby_excludes_corner_inside_bbox_but_outside_circle(session):
    # 'corner' sits inside the square bounding box (~445 m on each axis) but ~629 m away by
    # great-circle distance — so only the exact-distance trim can exclude it. This locks in
    # that the `<= radius_m` filter actually runs (the bbox prefilter alone wouldn't drop it).
    session.add_all(
        [
            Stop(stop_id="center", name="Center", lat=CS_LAT, lon=CS_LON),
            Stop(stop_id="corner", name="Corner", lat=CS_LAT + 0.004, lon=CS_LON + 0.006552),
        ]
    )
    await session.commit()

    results = await nearby_stops(session, lat=CS_LAT, lon=CS_LON, radius_m=500)

    ids = [stop.stop_id for stop, _ in results]
    assert "center" in ids
    assert "corner" not in ids  # inside the bbox, outside the 500 m circle


async def test_nearby_stops_filters_by_radius_and_orders_by_distance(session):
    session.add_all(
        [
            Stop(stop_id="near", name="Near", lat=CS_LAT + 0.0001, lon=CS_LON),  # ~11 m
            Stop(stop_id="mid", name="Mid", lat=CS_LAT + 0.003, lon=CS_LON),  # ~330 m
            Stop(stop_id="far", name="Far", lat=CS_LAT + 0.013, lon=CS_LON),  # ~1450 m
        ]
    )
    await session.commit()

    results = await nearby_stops(session, lat=CS_LAT, lon=CS_LON, radius_m=500)

    assert [stop.stop_id for stop, _ in results] == ["near", "mid"]  # 'far' excluded, sorted
    distances = [d for _, d in results]
    assert distances == sorted(distances)


async def test_stops_endpoint_returns_nearby_json(session):
    session.add_all(
        [
            Stop(stop_id="near", code="N", name="Near", lat=CS_LAT + 0.0001, lon=CS_LON),
            Stop(stop_id="far", name="Far", lat=CS_LAT + 0.013, lon=CS_LON),
        ]
    )
    await session.commit()

    async def _use_test_session():
        yield session

    app.dependency_overrides[get_session] = _use_test_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/stops", params={"lat": CS_LAT, "lon": CS_LON, "radius": 500}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [s["stop_id"] for s in body] == ["near"]  # 'far' is outside 500 m
    assert body[0]["name"] == "Near"
    assert body[0]["distance_m"] < 30


async def test_stops_endpoint_rejects_out_of_range_coordinates(session):
    async def _use_test_session():
        yield session

    app.dependency_overrides[get_session] = _use_test_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/stops", params={"lat": 999, "lon": 4.9, "radius": 500})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422  # latitude out of [-90, 90]


async def test_stops_endpoint_rejects_radius_over_cap(session):
    async def _use_test_session():
        yield session

    app.dependency_overrides[get_session] = _use_test_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/stops", params={"lat": CS_LAT, "lon": CS_LON, "radius": 99999}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422  # radius capped at 5000 by the endpoint
