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

    # Plannerstack demo by default; repoint at a self-hosted OTP later.
    otp_base_url: str = "https://otp.plannerstack.io/otp/routers/default"
    buienradar_url: str = "https://gpsgadget.buienradar.nl/data/raintext"

    # A single, deliberately short ceiling for every external call. Upstreams are
    # untrusted; we would rather fail fast and degrade than hang a request.
    request_timeout_seconds: float = 10.0

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached so `.env` is read once)."""
    return Settings()
