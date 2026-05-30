"""FastAPI dependency providers for the external clients.

Routing client construction through dependencies (rather than instantiating clients
inline in the endpoint) lets tests swap in clients pointed at fake hosts via
`app.dependency_overrides`, keeping endpoint tests hermetic and offline.
"""

from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.buienradar import BuienradarClient
from app.clients.geocoder import GeocoderClient
from app.clients.otp import OTPClient
from app.core.cache import Cache, RedisCache
from app.core.config import get_settings
from app.core.security import TokenError, decode_token
from app.db.session import get_session
from app.models.user import User
from app.services.rain import RainService

# OAuth2 password-bearer scheme. `tokenUrl` points at our token endpoint so the OpenAPI
# docs expose a working "Authorize" button; at runtime it only pulls the bearer token out
# of the Authorization header. It is module-level so FastAPI registers one shared scheme.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")


def get_otp_client() -> OTPClient:
    """Provide an OTP client configured from settings."""
    return OTPClient()


def get_buienradar_client() -> BuienradarClient:
    """Provide a Buienradar client configured from settings."""
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
