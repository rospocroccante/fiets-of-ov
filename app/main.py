"""FastAPI application entrypoint.

Builds the app and mounts routers. Per project convention, routers stay thin and
this module owns no business logic — it only wires things together.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import advice, auth, health, plan, stops, trip_alerts
from app.api.deps import get_buienradar_client, get_geocoder_client, get_otp_client
from app.core.config import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup; close the shared HTTP clients on shutdown.

    `configure_logging` is a guarded basicConfig, so under uvicorn's own log config
    it is a no-op — it only matters when nothing else set up the root logger.

    Nothing else starts eagerly: the client singletons build their AsyncClient lazily
    on first use, and `aclose()` on a never-used client is a no-op, so calling the
    providers here is safe whether or not the process ever served a request.
    """
    configure_logging()
    yield
    await get_otp_client().aclose()
    await get_buienradar_client().aclose()
    await get_geocoder_client().aclose()


app = FastAPI(title="Fiets of OV", version="0.0.0", lifespan=lifespan)

# /v1/plan responses carry full leg geometry and step lists (tens of kB of JSON), which
# gzip cuts severalfold; tiny bodies are left alone (compressing them costs more than it
# saves).
app.add_middleware(GZipMiddleware, minimum_size=1024)

# The browser frontend is served from its own origin, so every call to this API is
# cross-origin and needs CORS. Added last, which in Starlette means outermost: preflights
# are answered before anything else runs, and error responses still carry the headers the
# browser needs to show the real status instead of an opaque CORS failure.
#
# The allowlist is explicit config (never "*" — the settings reject it), which is what makes
# `allow_credentials=True` safe. We keep credentials on so a future cookie-based refresh flow
# works without revisiting this; today's bearer tokens only need the Authorization header
# through. Methods and headers are likewise enumerated rather than wildcarded: the routers
# only expose GET, POST and DELETE, so there is nothing to gain from opening it wider — a new
# verb should be a deliberate line here, not something a wildcard silently permitted.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(advice.router)
app.include_router(plan.router)
app.include_router(stops.router)
app.include_router(auth.router)
app.include_router(trip_alerts.router)
