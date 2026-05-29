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

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached so `.env` is read once)."""
    return Settings()
