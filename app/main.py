"""FastAPI application entrypoint.

Builds the app and mounts routers. Per project convention, routers stay thin and
this module owns no business logic — it only wires things together.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from app.api import advice, auth, health, plan, stops, trip_alerts
from app.api.deps import get_buienradar_client, get_geocoder_client, get_otp_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Close the shared HTTP clients on shutdown so pooled connections exit cleanly.

    Nothing starts eagerly: the client singletons build their AsyncClient lazily on
    first use, and `aclose()` on a never-used client is a no-op, so calling the
    providers here is safe whether or not the process ever served a request.
    """
    yield
    await get_otp_client().aclose()
    await get_buienradar_client().aclose()
    await get_geocoder_client().aclose()


app = FastAPI(title="Fiets of OV", version="0.0.0", lifespan=lifespan)

# /v1/plan responses carry full leg geometry and step lists (tens of kB of JSON), which
# gzip cuts severalfold; tiny bodies are left alone (compressing them costs more than it
# saves).
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(health.router)
app.include_router(advice.router)
app.include_router(plan.router)
app.include_router(stops.router)
app.include_router(auth.router)
app.include_router(trip_alerts.router)
