"""`GET /v1/plan` — the rich, drawable counterpart to `/v1/advice`.

Same inputs and same recommendation as `/v1/advice` (it reuses the geocoder, OTP and the
pure decision engine), but it also returns both full itineraries — bike and, when present,
public transport — each with per-leg geometry and detail. The client draws the route and
lists directions from this one response, so it never has to talk to OTP itself.
"""

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_cache, get_geocoder_client, get_otp_client, get_rain_service
from app.clients.geocoder import GeocodeNotFound, GeocoderClient
from app.clients.otp import Itinerary, Leg, OTPClient, OTPError
from app.core.cache import Cache
from app.schemas.plan import ItineraryOut, LegOut, OptionOut, PlaceOut, PlanResponse, StepOut
from app.services.advice import recommend
from app.services.places import resolve_place
from app.services.planner import gather_candidates_cached
from app.services.rain import RainService

router = APIRouter()


async def _resolve_place(value: str, geocoder: GeocoderClient) -> tuple[float, float]:
    """Resolve a `from`/`to` value to `(lat, lon)`, mapping domain errors to HTTP."""
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


def _leg_out(leg: Leg) -> LegOut:
    """Serialize an OTP leg into the public, drawable leg shape."""
    return LegOut(
        mode=leg.mode,
        minutes=_minutes(leg.duration),
        distance_m=round(leg.distance, 1) if leg.distance is not None else None,
        route=leg.route_short_name,
        route_long_name=leg.route_long_name,
        headsign=leg.headsign,
        origin=PlaceOut(name=leg.from_name, lat=leg.from_lat, lon=leg.from_lon),
        to=PlaceOut(name=leg.to_name, lat=leg.to_lat, lon=leg.to_lon),
        geometry=leg.geometry,
        start_time=leg.start_time,
        end_time=leg.end_time,
        steps=[
            StepOut(distance_m=s.distance, direction=s.relative_direction, street=s.street_name)
            for s in leg.steps
        ],
    )


def _itinerary_out(itin: Itinerary) -> ItineraryOut:
    """Serialize an OTP itinerary, summing the leg distances for a total."""
    distance = sum((leg.distance or 0.0) for leg in itin.legs)
    return ItineraryOut(
        minutes=_minutes(itin.duration),
        distance_m=round(distance, 1),
        start_time=itin.start_time,
        end_time=itin.end_time,
        legs=[_leg_out(leg) for leg in itin.legs],
    )


@router.get("/v1/plan", response_model=PlanResponse)
async def get_plan(
    origin: str = Query(alias="from", description="Origin: place name or 'lat,lon'"),
    destination: str = Query(alias="to", description="Destination: place name or 'lat,lon'"),
    otp: OTPClient = Depends(get_otp_client),
    rain_service: RainService = Depends(get_rain_service),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
    cache: Cache = Depends(get_cache),
) -> PlanResponse:
    """Return the rain-aware recommendation plus all ranked, drawable options."""
    # The two geocodes are independent, so resolve them concurrently. The first failure
    # propagates with its existing status mapping (400/502 from _resolve_place); gather
    # retrieves the sibling's outcome internally, so nothing is left unawaited.
    from_place, to_place = await asyncio.gather(
        _resolve_place(origin, geocoder), _resolve_place(destination, geocoder)
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
        raise HTTPException(status_code=502, detail="no route found for this trip")

    plan = recommend(candidates, rain)

    return PlanResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
        origin=PlaceOut(lat=from_place[0], lon=from_place[1]),
        destination=PlaceOut(lat=to_place[0], lon=to_place[1]),
        options=[
            OptionOut(
                kind=opt.kind,
                recommended=(i == 0),
                score=opt.cost,
                rain_minutes=opt.rain_minutes,
                itinerary=_itinerary_out(opt.itinerary),
            )
            for i, opt in enumerate(plan.options)
        ],
    )
