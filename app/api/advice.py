"""`GET /v1/advice` — the rain-aware bike-vs-OV endpoint.

Thin controller, per project convention: it resolves the trip endpoints (either
`lat,lon` or a place name geocoded via Nominatim), gathers the bike and transit
itineraries (OTP) and the rain forecast (Buienradar) through the client layer, hands
them to the pure decision engine, and returns the result. All decision logic lives in
`app/services/advice.py`, not here.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_cache,
    get_geocoder_client,
    get_otp_client,
    get_rain_service,
    resolve_place_http,
)
from app.clients.geocoder import GeocoderClient
from app.clients.otp import OTPClient, OTPError
from app.core.cache import Cache
from app.schemas.advice import AdviceResponse
from app.services.advice import recommend
from app.services.planner import gather_candidates_cached
from app.services.rain import RainService

router = APIRouter()


@router.get("/v1/advice", response_model=AdviceResponse)
async def get_advice(
    origin: str = Query(alias="from"),
    destination: str = Query(alias="to"),
    otp: OTPClient = Depends(get_otp_client),
    rain_service: RainService = Depends(get_rain_service),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
    cache: Cache = Depends(get_cache),
) -> AdviceResponse:
    """Return a rain-aware bike / transit / bike-and-ride recommendation for the trip."""
    # The two geocodes are independent, so resolve them concurrently. The first failure
    # propagates with its existing status mapping (400/502 from resolve_place_http);
    # gather retrieves the sibling's outcome internally, so nothing is left unawaited.
    from_place, to_place = await asyncio.gather(
        resolve_place_http(origin, geocoder), resolve_place_http(destination, geocoder)
    )

    try:
        # Routing and the rain forecast only share the origin, so they run concurrently.
        # get_forecast degrades to None instead of raising, so the only exception here
        # is OTPError — mapped to the same 502 as before.
        candidates, rain = await asyncio.gather(
            gather_candidates_cached(otp, cache, from_place, to_place),
            rain_service.get_forecast(lat=from_place[0], lon=from_place[1]),
        )
    except OTPError as exc:
        raise HTTPException(status_code=502, detail="routing upstream unavailable") from exc
    if not candidates:
        # OTP answered, it just has no way to make this trip (endpoints off the network, an
        # unreachable island, a walk-only result). That is a fact about the request, not an
        # outage, so it must not share the 502 that means "our upstream is broken" — a client
        # should tell the user to pick another destination, and a monitor should not page.
        raise HTTPException(status_code=404, detail="no route found for this trip")

    plan = recommend(candidates, rain)

    return AdviceResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        bike_minutes=plan.bike_minutes,
        transit_minutes=plan.transit_minutes,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
        forecast_degraded=plan.forecast_degraded,
    )
