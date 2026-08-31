"""Application settings, loaded from the environment (and `.env`).

Centralises configuration so nothing reads `os.environ` ad hoc. External base URLs
live here rather than being hardcoded in the clients, so they can be repointed
(e.g. at a self-hosted OTP in a later phase) without touching code.
"""

import json
from functools import lru_cache
from typing import Annotated, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The development placeholder for JWT_SECRET. Bound to a name (rather than inlined as the
# field default) so the production guard can recognise it by value: this string is public in
# the repository, so a deployment that forgot to override it must fail loudly rather than
# quietly sign tokens anyone can forge.
DEV_JWT_SECRET = "dev-insecure-change-me-override-in-production"

# HS256's signature is a SHA-256 HMAC, so a key shorter than the 32-byte hash output adds no
# strength and trips PyJWT's InsecureKeyLengthWarning. In production we refuse it outright.
MIN_JWT_SECRET_BYTES = 32

# Environments treated as a developer sandbox, where the placeholder secret is allowed so
# `make run` and the offline test suite work with zero configuration. Everything else —
# "prod", "staging", or a typo — is treated as production, so an unrecognised APP_ENV fails
# safe (guards on) rather than open.
DEV_APP_ENVS = frozenset({"dev", "test"})


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case env vars (see `.env.example`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Which kind of deployment this process is. It gates the production-only guards below;
    # defaulting to "dev" keeps local development configuration-free, while a real
    # deployment only has to set APP_ENV=prod to get the strict behaviour.
    app_env: str = "dev"

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

    # Overall deadline for the pedestrian-deck bike-snapping fallback (see
    # app/services/snap.py). The fallback can fan out up to 24 OTP probe queries; past
    # this ceiling we accept "no bike option" rather than hold the request hostage to a
    # slow OTP.
    snap_timeout_seconds: float = 8.0

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
    # Buienradar's horizon is ~2h from fetch time and advice reasons over the *next*
    # ~30-60 min, so a 2h-old forecast no longer covers the ride at all — yet a stale
    # hit is served as a confident, non-degraded answer. 45 min keeps ~1h of genuine
    # horizon over the cycling window; beyond that, degrading honestly beats guessing.
    rain_cache_retention_seconds: int = 2700

    # Browser origins allowed to call this API cross-origin. Defaults to the Vite dev server
    # so a locally running frontend works out of the box. Always an explicit allowlist: the
    # CORS middleware advertises credentials support, and "*" would make it echo back
    # whatever Origin asked — see `_reject_wildcard_origin`.
    #
    # NoDecode turns off pydantic-settings' automatic JSON decoding for this field, which
    # would otherwise reject `CORS_ALLOW_ORIGINS=http://a,http://b` before any validator
    # sees it. Parsing moves to `_parse_origin_list` instead, which takes both that form and
    # a JSON array — a list of URLs is the one setting people actually write by hand, and
    # JSON-quoting it inside a compose file or a systemd unit is a needless trap.
    cors_allow_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # JWT auth (Phase 5). The secret signs/validates access tokens; the dev default is a
    # placeholder and MUST be overridden in prod via JWT_SECRET — outside dev/test the
    # settings refuse to build with it (see `_require_production_grade_jwt_secret`). It is
    # ≥32 bytes so it clears PyJWT's HS256 key-length floor — a genuinely short prod secret
    # still triggers PyJWT's InsecureKeyLengthWarning (we no longer suppress it). HS256 is
    # symmetric, which fits a single-service deployment (no need to distribute a public key).
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    # Access-token lifetime. Kept short-ish so a leaked token's blast radius is bounded;
    # there is no refresh-token flow in the MVP, so this is the full session length.
    access_token_expire_minutes: int = 60

    # Background rain notifications (Phase 6). The ARQ worker wakes on a cron schedule and
    # alerts a trip's owner when rain is expected on the bike leg around departure.
    # How far ahead of a trip's departure_time we look when deciding to notify — the alert
    # should arrive with enough lead to act on (grab the tram instead of the bike), but not
    # so early that the short-term rain forecast is unreliable. 15 min balances the two.
    notify_lead_minutes: int = 15
    # Cron cadence for the worker's due-check sweep, in seconds (300 = every 5 min). Matches
    # Buienradar's ~5-min update cadence, so we never re-evaluate against unchanged data.
    notify_scheduler_seconds: int = 300

    log_level: str = "INFO"

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_origin_list(cls, value: object) -> object:
        """Parse the origin allowlist from either a comma-separated string or a JSON array.

        This field opts out of pydantic-settings' JSON decoding (see `NoDecode` above), so
        the raw env string lands here. A leading `[` means someone wrote JSON and we honour
        it; anything else is split on commas, which is how origin lists are normally typed.
        Non-strings (an in-code default, an explicit list) pass straight through.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [origin.strip() for origin in text.split(",") if origin.strip()]

    @field_validator("cors_allow_origins", mode="after")
    @classmethod
    def _reject_wildcard_origin(cls, origins: list[str]) -> list[str]:
        """Refuse `*`, in every environment.

        The CORS middleware is mounted with credentials support, and Starlette answers a
        wildcard allowlist by reflecting the caller's own Origin — which would let any site
        on the internet drive an authenticated request against this API. There is no
        configuration in which we want that, so it is a hard error rather than a convention.
        """
        if "*" in origins:
            raise ValueError("CORS_ALLOW_ORIGINS must be an explicit allowlist, never '*'")
        return origins

    @model_validator(mode="after")
    def _require_production_grade_jwt_secret(self) -> Self:
        """Outside dev/test, refuse to build settings with a forgeable signing secret.

        This class of misconfiguration is silent in the worst way: the service starts
        happily and issues perfectly valid tokens that anyone with a copy of this repository
        can forge. Failing here means the process dies at the first `get_settings()` — i.e.
        during app import, before it can ever serve a request — with a message that names
        the fix, instead of at some later audit.

        Only `dev`/`test` are exempt, which is what keeps local development and the offline
        test suite running on the placeholder with no configuration at all.
        """
        if self.app_env.strip().lower() in DEV_APP_ENVS:
            return self
        if self.jwt_secret == DEV_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is still the development placeholder. Set a strong random "
                "secret (e.g. `openssl rand -hex 32`), or run with APP_ENV=dev."
            )
        if len(self.jwt_secret.encode()) < MIN_JWT_SECRET_BYTES:
            raise ValueError(
                f"JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes outside dev/test "
                "(HS256 signs with SHA-256; a shorter key adds no strength)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached so `.env` is read once)."""
    return Settings()
