"""`/v1/trip-alerts` — a user's saved A→B trips for rain-aware notifications (Phase 5).

Thin controller, per project convention: it authenticates via `get_current_user`, resolves
the free-text origin/destination to coordinates through the `resolve_place` service, and
persists/reads rows through the request-scoped session. No decision or geocoding logic lives
here — only HTTP shaping and ownership checks.

Every route requires a bearer token. Ownership is enforced on read *and* delete: a user only
ever sees or removes their own trip alerts, so the list filters by `user_id` and the delete
404s on a row that exists but belongs to someone else (same response as a missing row — we
never reveal that another user's trip alert exists).
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_geocoder_client
from app.clients.geocoder import GeocodeNotFound, GeocoderClient
from app.db.session import get_session
from app.models.trip_alert import TripAlert
from app.models.user import User
from app.schemas.trip_alert import TripAlertCreate, TripAlertOut
from app.services.places import resolve_place

router = APIRouter(prefix="/v1/trip-alerts", tags=["trip-alerts"])


async def _resolve_place(value: str, geocoder: GeocoderClient) -> tuple[float, float]:
    """Resolve an origin/destination value to `(lat, lon)`, mapping domain errors to HTTP.

    Shares the parse-or-geocode logic with `/v1/advice` via `resolve_place`; this wrapper
    only translates the domain errors into status codes: an unresolvable name is the
    caller's mistake (400), a geocoder outage is an upstream failure (502).
    """
    try:
        return await resolve_place(value, geocoder)
    except GeocodeNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"could not find a place named {value!r}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="geocoding upstream unavailable",
        ) from exc


@router.post("", response_model=TripAlertOut, status_code=status.HTTP_201_CREATED)
async def create_trip_alert(
    body: TripAlertCreate,
    user: User = Depends(get_current_user),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
    session: AsyncSession = Depends(get_session),
) -> TripAlert:
    """Create a trip alert for the current user, resolving both endpoints to coordinates.

    The origin and destination are geocoded once at create time (400 if a name can't be
    resolved, 502 if the geocoder upstream is down) so the notification worker never has to
    geocode again. The row is owned by the authenticated user — `user_id` is taken from the
    token, never from the body, so a client can't create trip alerts for someone else.
    """
    origin_lat, origin_lon = await _resolve_place(body.origin, geocoder)
    dest_lat, dest_lon = await _resolve_place(body.destination, geocoder)

    trip_alert = TripAlert(
        user_id=user.id,
        label=body.label,
        origin=body.origin,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        destination=body.destination,
        dest_lat=dest_lat,
        dest_lon=dest_lon,
        departure_time=body.departure_time,
        days=body.days,
    )
    session.add(trip_alert)
    await session.commit()
    # Server-default columns (id, created_at) are populated on commit and expire_on_commit
    # is False, so the instance is safe to serialize directly without a re-read.
    return trip_alert


@router.get("", response_model=list[TripAlertOut])
async def list_trip_alerts(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TripAlert]:
    """List the current user's trip alerts, newest first.

    Scoped to `user_id` so the response only ever contains the caller's own rows. Ordered by
    descending id (a monotonic surrogate key) so the most recently created appears first
    without depending on `created_at` tie-breaking within the same timestamp.
    """
    result = await session.execute(
        select(TripAlert).where(TripAlert.user_id == user.id).order_by(TripAlert.id.desc())
    )
    return list(result.scalars().all())


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip_alert(
    # Bound to the Postgres int4 range: an id beyond it can't exist, so reject it as 422
    # rather than letting asyncpg raise "out of int32 range" and 500 the request.
    alert_id: int = Path(ge=1, le=2_147_483_647),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete one of the current user's trip alerts.

    The delete is scoped to both `id` and `user_id`, so a row that exists but belongs to
    another user matches nothing — yielding the same 404 as a genuinely missing row. That
    keeps one user from learning about (or removing) another's trip alerts.
    """
    result = await session.execute(
        select(TripAlert).where(TripAlert.id == alert_id, TripAlert.user_id == user.id)
    )
    trip_alert = result.scalar_one_or_none()
    if trip_alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip alert not found")
    await session.delete(trip_alert)
    await session.commit()
