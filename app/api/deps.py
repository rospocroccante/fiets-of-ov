"""FastAPI dependency providers for the external clients.

Routing client construction through dependencies (rather than instantiating clients
inline in the endpoint) lets tests swap in clients pointed at fake hosts via
`app.dependency_overrides`, keeping endpoint tests hermetic and offline.

Also home to `resolve_place_http`, the router-shared wrapper that maps the place
service's domain errors onto HTTP status codes — the services layer stays
HTTP-agnostic (see app/services/places.py), so the mapping lives here in the API layer.
"""

from functools import lru_cache

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.buienradar import BuienradarClient
from app.clients.geocoder import GeocodeNotFound, GeocoderClient
from app.clients.otp import OTPClient
from app.core.cache import Cache, RedisCache
from app.core.config import get_settings
from app.core.security import TokenError, decode_token
from app.db.session import get_session
from app.models.user import User
from app.services.places import resolve_place
from app.services.rain import RainService

# OAuth2 password-bearer scheme. `tokenUrl` points at our token endpoint so the OpenAPI
# docs expose a working "Authorize" button; at runtime it only pulls the bearer token out
# of the Authorization header. It is module-level so FastAPI registers one shared scheme.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")


@lru_cache
def get_otp_client() -> OTPClient:
    """Provide the process-wide OTP client.

    A singleton so its lazily created shared AsyncClient (SSL context + connection pool)
    is reused across requests instead of rebuilt per call; the app lifespan closes it on
    shutdown (see app/main.py).
    """
    return OTPClient()


@lru_cache
def get_buienradar_client() -> BuienradarClient:
    """Provide the process-wide Buienradar client (singleton for the same pooled
    AsyncClient reuse as `get_otp_client`)."""
    return BuienradarClient()


@lru_cache
def get_geocoder_client() -> GeocoderClient:
    """Provide a process-wide geocoder client.

    Cached as a singleton (unlike the others) so its in-process place-name cache
    survives across requests, sparing Nominatim repeat lookups for the same names.
    """
    return GeocoderClient()


@lru_cache
def get_cache() -> Cache:
    """Provide the process-wide Redis cache (singleton, so its connection pool is reused)."""
    return RedisCache()


def get_rain_service() -> RainService:
    """Provide the rain service: Buienradar wrapped in caching + graceful degradation."""
    settings = get_settings()
    return RainService(
        get_buienradar_client(),
        get_cache(),
        fresh_seconds=settings.rain_cache_fresh_seconds,
        retention_seconds=settings.rain_cache_retention_seconds,
    )


async def resolve_place_http(value: str, geocoder: GeocoderClient) -> tuple[float, float]:
    """Resolve a `from`/`to` value to `(lat, lon)`, mapping domain errors to HTTP.

    Delegates the parse-or-geocode decision to `resolve_place` (deliberately
    HTTP-agnostic); this wrapper owns the one status mapping every router shares: an
    unresolvable name is the caller's mistake (400), a geocoder outage is an upstream
    failure (502). Shared by /v1/advice, /v1/plan and /v1/trip-alerts so the three
    endpoints cannot drift apart in how they report geocoding failures.
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


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the bearer token to the authenticated `User`, or raise 401.

    The token's subject is the user id (set at login as `str(user.id)`); we decode it,
    then load the matching row. Both a bad/expired token and a missing user yield the same
    opaque 401 — we deliberately don't distinguish "invalid token" from "user gone" so the
    endpoint leaks no information about which accounts exist.

    Raises:
        HTTPException(401): on any token failure or if no such user exists. The
            `WWW-Authenticate: Bearer` header is what the OAuth2 spec requires so clients
            know to retry with a bearer credential.
    """
    # A single 401 covers every failure mode below; build it once to keep them identical.
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        subject = decode_token(token)
    except TokenError as exc:
        raise credentials_error from exc

    # `sub` is a string by JWT convention; the PK is an int, so coerce. A non-numeric
    # subject means a token we never issued, which is just another invalid credential.
    try:
        user_id = int(subject)
    except ValueError as exc:
        raise credentials_error from exc

    user = await session.get(User, user_id)
    if user is None:
        # Valid signature but the account no longer exists (e.g. deleted after the token
        # was issued) — treat it as an unauthenticated request, not a 404.
        raise credentials_error
    return user
