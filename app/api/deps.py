"""FastAPI dependency providers for the external clients.

Routing client construction through dependencies (rather than instantiating clients
inline in the endpoint) lets tests swap in clients pointed at fake hosts via
`app.dependency_overrides`, keeping endpoint tests hermetic and offline.
"""

from functools import lru_cache

from app.clients.buienradar import BuienradarClient
from app.clients.geocoder import GeocoderClient
from app.clients.otp import OTPClient
from app.core.cache import Cache, RedisCache
from app.core.config import get_settings
from app.services.rain import RainService


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
