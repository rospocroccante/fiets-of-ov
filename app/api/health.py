"""Liveness endpoint.

Kept dependency-free on purpose: `/health` must answer even when Postgres, Redis
or the upstream APIs are down, so orchestrators (docker compose, CI, a deploy
platform) can tell the process is up without dragging in those checks. A deeper
readiness probe that pings dependencies can come later as a separate route.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Return a static liveness payload. No I/O by design."""
    return {"status": "ok"}
