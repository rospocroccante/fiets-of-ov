"""`GET /v1/advice` — the rain-aware bike-vs-OV endpoint.

Thin controller, per project convention: it resolves the trip endpoints (either
`lat,lon` or a place name geocoded via Nominatim), gathers the bike and transit
itineraries (OTP) and the rain forecast (Buienradar) through the client layer, hands
them to the pure decision engine, and returns the result. All decision logic lives in
`app/services/advice.py`, not here.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_geocoder_client, get_otp_client, get_rain_service
from app.clients.geocoder import GeocodeNotFound, GeocoderClient
from app.clients.otp import OTPClient, OTPError
from app.schemas.advice import AdviceResponse
from app.services.advice import recommend
from app.services.places import resolve_place
from app.services.planner import gather_candidates
from app.services.rain import RainService

router = APIRouter()


async def _resolve_place(value: str, geocoder: GeocoderClient) -> tuple[float, float]:
    """Resolve a `from`/`to` value to `(lat, lon)`, mapping domain errors to HTTP.

    Delegates the parse-or-geocode decision to `resolve_place`; this wrapper only
    translates its domain errors into HTTP status codes: an unresolvable name is the
    caller's mistake (400), a geocoder outage is an upstream failure (502).
    """
    try:
        return await resolve_place(value, geocoder)
    except GeocodeNotFound as exc:
        raise HTTPException(
            status_code=400, detail=f"could not find a place named {value!r}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="geocoding upstream unavailable") from exc


def _minutes(seconds: float) -> int:
    """Round a duration in seconds to whole minutes for human-facing output."""
    return round(seconds / 60)


@router.get("/v1/advice", response_model=AdviceResponse)
async def get_advice(
    origin: str = Query(alias="from"),
    destination: str = Query(alias="to"),
    otp: OTPClient = Depends(get_otp_client),
    rain_service: RainService = Depends(get_rain_service),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
) -> AdviceResponse:
    """Return a rain-aware bike / transit / bike-and-ride recommendation for the trip."""
    from_place = await _resolve_place(origin, geocoder)
    to_place = await _resolve_place(destination, geocoder)

    try:
        candidates = await gather_candidates(otp, from_place, to_place)
    except OTPError as exc:
        raise HTTPException(status_code=502, detail="routing upstream unavailable") from exc
    if not candidates:
        raise HTTPException(status_code=502, detail="no route found for this trip")

    rain = await rain_service.get_forecast(lat=from_place[0], lon=from_place[1])
    plan = recommend(candidates, rain)

    return AdviceResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        bike_minutes=plan.bike_minutes
        if plan.bike_minutes is not None
        else _minutes(plan.options[0].itinerary.duration),
        transit_minutes=plan.transit_minutes,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
    )
