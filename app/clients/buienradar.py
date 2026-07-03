"""Async client for Buienradar's `raintext` precipitation feed.

Quirks worth knowing (and the reason this wrapper exists):

- The endpoint returns **plain text**, not JSON: one line per ~5-minute slot for the
  next ~2 hours, formatted `iii|HH:MM` (e.g. `077|16:05`).
- `iii` is a 0-255 *intensity code*, not mm/h and not a yes/no flag. Buienradar's
  published conversion is `mm/h = 10 ** ((code - 109) / 32)`. We expose mm/h so the
  decision engine never has to know about codes. Code `0` is treated as exactly
  dry (the formula would otherwise yield a meaningless ~0.0004 mm/h).
- Times are local (Europe/Amsterdam) wall-clock `HH:MM` with no date. Reconciling
  them with absolute itinerary timestamps is the decision engine's job, not the
  client's — here we just parse them into `datetime.time`.

No caching here yet; a ~5 min TTL belongs to the caching phase. Every call is bounded
by an explicit timeout: Buienradar is untrusted, so we fail fast rather than hang.
"""

from datetime import time

import httpx
from pydantic import BaseModel

from app.core.config import get_settings


class RainSlot(BaseModel):
    """A single forecast slot: local time, raw intensity code, and converted mm/h."""

    time: time
    intensity: int
    mm_per_h: float


class RainForecast(BaseModel):
    """The parsed `raintext` response: an ordered list of ~5-minute slots."""

    slots: list[RainSlot]


def _intensity_to_mm_per_h(code: int) -> float:
    """Convert a Buienradar 0-255 intensity code to mm/h.

    Uses Buienradar's published formula `10 ** ((code - 109) / 32)`. Code 0 is special
    cased to 0.0 — it means "no rain", and running it through the formula would produce
    a tiny non-zero value that misleadingly reads as drizzle.
    """
    if code <= 0:
        return 0.0
    return round(10 ** ((code - 109) / 32), 4)


def _parse_raintext(text: str) -> list[RainSlot]:
    """Parse the `iii|HH:MM` lines into slots, skipping blank/whitespace lines."""
    slots: list[RainSlot] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_intensity, raw_time = line.split("|", 1)
        code = int(raw_intensity)
        hour, minute = (int(part) for part in raw_time.split(":", 1))
        slots.append(
            RainSlot(
                time=time(hour=hour, minute=minute),
                intensity=code,
                mm_per_h=_intensity_to_mm_per_h(code),
            )
        )
    if not slots:
        # A blank/whitespace-only body is a broken response, not a dry forecast. Returning
        # an empty forecast would make the engine confidently report "dry"; raise instead
        # so the rain service degrades to an honest "unavailable".
        raise ValueError("empty raintext response")
    return slots


class BuienradarClient:
    """Fetches and parses the Buienradar `raintext` forecast for a coordinate."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self._base_url = base_url or settings.buienradar_url
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds
        # One shared AsyncClient, created lazily on first use so instantiation needs no
        # event loop; per-request construction would rebuild the SSL context every call
        # and never reuse a connection.
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        """The shared HTTP client, created on first use."""
        if self._client is None:
            # Buienradar 302-redirects fine-grained coordinates to a rounded,
            # trailing-slash URL, so the client must follow redirects or every real
            # request fails on the 302.
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the shared HTTP client and its pooled connections, if one was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_rain_forecast(self, *, lat: float, lon: float) -> RainForecast:
        """Return the ~2h precipitation forecast for `(lat, lon)` as typed mm/h slots.

        Raises `httpx.HTTPError` if the request fails or returns a non-2xx status, or
        `ValueError` if a 2xx body can't be parsed as raintext (including an empty body).
        Callers decide how to degrade — the rain service treats both as "unavailable".
        """
        response = await self._http().get(self._base_url, params={"lat": lat, "lon": lon})
        response.raise_for_status()
        return RainForecast(slots=_parse_raintext(response.text))
