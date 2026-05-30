"""Application settings, loaded from the environment (and `.env`).

Centralises configuration so nothing reads `os.environ` ad hoc. External base URLs
live here rather than being hardcoded in the clients, so they can be repointed
(e.g. at a self-hosted OTP in a later phase) without touching code.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case env vars (see `.env.example`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Async SQLAlchemy URL for Postgres (asyncpg driver). Holds the GVB stops (Phase 4).
    database_url: str = "postgresql+asyncpg://fiets:fiets@localhost:5432/fiets"

    # Host root of a self-hosted OTP2 instance; the client appends the GTFS GraphQL path.
    otp_base_url: str = "http://localhost:8080"
    buienradar_url: str = "https://gpsgadget.buienradar.nl/data/raintext"

    # Nominatim forward-geocoding (place name -> coordinates). Its usage policy requires
    # an identifying User-Agent; override the default to something contactable in prod.
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    geocoder_user_agent: str = "fiets-of-ov/0.1 (https://github.com/fiets-of-ov)"

    # A single, deliberately short ceiling for every external call. Upstreams are
    # untrusted; we would rather fail fast and degrade than hang a request.
    request_timeout_seconds: float = 10.0

    # Redis-backed cache for the rain forecast. The cache fails open: if Redis is down
    # the request still runs (just without caching), so this is never a hard dependency.
    redis_url: str = "redis://localhost:6379/0"
    # Hard socket/connect ceiling for Redis. The cache is hit on every request, so a
    # wedged-but-connected Redis must not hang it — kept short (a slow cache is no help).
    redis_timeout_seconds: float = 2.0
    # Within this window a cached forecast is served without re-hitting Buienradar
    # (~5 min matches Buienradar's own update cadence).
    rain_cache_fresh_seconds: int = 300
    # How long a forecast is retained for stale-fallback use when Buienradar is down.
    # Beyond ~2h the forecast horizon is exhausted, so keeping it longer is pointless.
    rain_cache_retention_seconds: int = 7200

    # JWT auth (Phase 5). The secret signs/validates access tokens; the dev default is a
    # placeholder and MUST be overridden in prod via JWT_SECRET. It is ≥32 bytes so it
    # clears PyJWT's HS256 key-length floor — a genuinely short prod secret still triggers
    # PyJWT's InsecureKeyLengthWarning (we no longer suppress it). HS256 is symmetric,
    # which fits a single-service deployment (no need to distribute a public key).
    jwt_secret: str = "dev-insecure-change-me-override-in-production"
    jwt_algorithm: str = "HS256"
    # Access-token lifetime. Kept short-ish so a leaked token's blast radius is bounded;
    # there is no refresh-token flow in the MVP, so this is the full session length.
    access_token_expire_minutes: int = 60

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached so `.env` is read once)."""
    return Settings()
