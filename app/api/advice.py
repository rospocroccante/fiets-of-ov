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
from app.clients.otp import OTPClient, OTPError, first_transit_itinerary
from app.schemas.advice import AdviceResponse
from app.services.advice import decide
from app.services.rain import RainService

router = APIRouter()


def _try_parse_latlon(value: str) -> tuple[float, float] | None:
    """Parse a `"lat,lon"` value into floats, or None if it isn't a coordinate pair."""
    try:
        lat_str, lon_str = value.split(",")
        return float(lat_str), float(lon_str)
    except ValueError:
        return None


async def _resolve_place(value: str, geocoder: GeocoderClient) -> tuple[float, float]:
    """Resolve a `from`/`to` value to `(lat, lon)`.

    Accepts either explicit `"lat,lon"` coordinates (resolved locally, no network) or a
    place name (geocoded via Nominatim). An unresolvable name is the caller's mistake
    (HTTP 400); a geocoder outage is an upstream failure (HTTP 502).
    """
    coords = _try_parse_latlon(value)
    if coords is not None:
        return coords
    try:
        return await geocoder.geocode(value)
    except GeocodeNotFound as exc:
        raise HTTPException(
            status_code=400, detail=f"could not find a place named {value!r}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="geocoding upstream unavailable") from exc


@router.get("/v1/advice", response_model=AdviceResponse)
async def get_advice(
    origin: str = Query(
        alias="from", description="Origin: place name (e.g. 'Vondelpark') or 'lat,lon'"
    ),
    destination: str = Query(
        alias="to", description="Destination: place name (e.g. 'Vondelpark') or 'lat,lon'"
    ),
    otp: OTPClient = Depends(get_otp_client),
    rain_service: RainService = Depends(get_rain_service),
    geocoder: GeocoderClient = Depends(get_geocoder_client),
) -> AdviceResponse:
    """Return a bike-vs-OV recommendation for the trip `from` -> `to`."""
    from_place = await _resolve_place(origin, geocoder)
    to_place = await _resolve_place(destination, geocoder)

    # Bike routing is essential: if OTP can't give us a bike option, there's no
    # advice to render, so surface a clear upstream error rather than guess.
    try:
        bike_plan = await otp.plan(from_place=from_place, to_place=to_place, mode="BICYCLE")
    except OTPError as exc:
        raise HTTPException(status_code=502, detail="routing upstream unavailable") from exc
    if not bike_plan.itineraries:
        raise HTTPException(status_code=502, detail="no bike route found for this trip")
    bike = bike_plan.itineraries[0]

    # Transit is best-effort: "no public-transport option" is a valid answer the engine
    # handles, not an error. Swallow OTP failures here and fall back to no transit. We
    # also skip WALK-only itineraries (see first_transit_itinerary): a walk is no drier
    # than the bike, so it must not pose as the public-transport alternative.
    transit = None
    try:
        transit_plan = await otp.plan(from_place=from_place, to_place=to_place, mode="TRANSIT,WALK")
        transit = first_transit_itinerary(transit_plan)
    except OTPError:
        transit = None

    # Rain is overlaid at the origin. The rain service handles caching and graceful
    # degradation: if Buienradar is down with no usable cache it returns None, and the
    # engine still answers (bike-first, forecast flagged unknown). So we never 502 here —
    # one flaky upstream must not take down the whole recommendation.
    rain = await rain_service.get_forecast(lat=from_place[0], lon=from_place[1])

    return decide(bike=bike, transit=transit, rain=rain)
