"""FastAPI application entrypoint.

Builds the app and mounts routers. Per project convention, routers stay thin and
this module owns no business logic — it only wires things together.
"""

from fastapi import FastAPI

from app.api import advice, auth, health, plan, stops, trip_alerts

app = FastAPI(title="Fiets of OV", version="0.0.0")

app.include_router(health.router)
app.include_router(advice.router)
app.include_router(plan.router)
app.include_router(stops.router)
app.include_router(auth.router)
app.include_router(trip_alerts.router)
