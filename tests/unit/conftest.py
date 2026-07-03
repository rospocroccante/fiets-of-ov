"""Shared harness for the endpoint suites (test_advice_endpoint, test_plan_endpoint).

Both suites drive the same app wiring — OTP's GraphQL `plan` and Buienradar's raintext
mocked with respx, the clients swapped via FastAPI dependency overrides — so the
infrastructure lives here once: URL constants, the epoch-ms/GraphQL payload helpers, the
fake geocoder, the rain service stub, the per-mode-set OTP dispatcher and the
`_override_clients` fixture. Each test module keeps its own OTP payloads local (the two
endpoints assert different itinerary shapes) and opts into the overrides with
`pytestmark = pytest.mark.usefixtures("_override_clients")`, so unrelated unit tests are
untouched.

The helpers are imported by the test modules (`from tests.unit.conftest import ...`):
they build module-level payload constants, which a fixture cannot do.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.api.deps import get_cache, get_geocoder_client, get_otp_client, get_rain_service
from app.clients.buienradar import BuienradarClient
from app.clients.geocoder import GeocodeNotFound
from app.clients.otp import OTPClient
from app.core.cache import InMemoryCache
from app.main import app
from app.services.rain import RainService

TZ = ZoneInfo("Europe/Amsterdam")
OTP_URL = "http://otp.test"  # host root; client appends /otp/gtfs/v1
GQL_URL = f"{OTP_URL}/otp/gtfs/v1"
RAIN_URL = "https://rain.test/raintext"

# 109|14:05 -> 1.0 mm/h (wet); everything else dry. The bike itineraries' epoch-ms times
# are anchored to the same fixed June day (see `ms`) so the engine sees them as aligned.
RAIN_WET = "000|14:00\n109|14:05\n000|14:10\n000|14:20\n"
RAIN_DRY = "000|14:00\n000|14:05\n000|14:20\n"


def ms(hour: int, minute: int) -> int:
    """Epoch-ms for the fixed test day (2026-06-01) at Amsterdam wall-clock time."""
    return int(datetime(2026, 6, 1, hour, minute, tzinfo=TZ).timestamp() * 1000)


def _gql(itineraries: list[dict]) -> dict:
    """Wrap itineraries in the GraphQL `plan` response envelope the OTP client parses."""
    return {"data": {"plan": {"itineraries": itineraries}}}


class _FakeGeocoder:
    """Stand-in geocoder: resolves a couple of known names, raises otherwise."""

    _PLACES = {"amsterdam centraal": (52.3791, 4.9003), "vondelpark": (52.3579, 4.8686)}

    async def geocode(self, query: str) -> tuple[float, float]:
        key = query.strip().lower()
        if key not in self._PLACES:
            raise GeocodeNotFound(query)
        return self._PLACES[key]


def _rain_service() -> RainService:
    # Real rain service over a fresh in-memory cache, pointed at the mocked Buienradar.
    # Exercises the actual caching + degradation path instead of stubbing it out.
    return RainService(
        BuienradarClient(base_url=RAIN_URL),
        InMemoryCache(),
        fresh_seconds=300,
        retention_seconds=7200,
    )


@pytest.fixture
def _override_clients():
    """Point the app's clients at the fakes above for the duration of one test.

    Not autouse: only the endpoint suites opt in (via module-level usefixtures), so the
    rest of the unit suite runs against the app's real dependency wiring.
    """
    # A fresh in-memory plan cache per test: many tests reuse identical coordinates with
    # different OTP mocks, so hitting a real local Redis would leak plans across tests.
    plan_cache = InMemoryCache()
    app.dependency_overrides[get_otp_client] = lambda: OTPClient(base_url=OTP_URL)
    app.dependency_overrides[get_rain_service] = _rain_service
    app.dependency_overrides[get_geocoder_client] = _FakeGeocoder
    app.dependency_overrides[get_cache] = lambda: plan_cache
    yield
    app.dependency_overrides.clear()


def _otp_by_modes(by_key: dict[str, httpx.Response]):
    """respx side_effect answering by the full requested mode set (comma-joined)."""

    def handler(request: httpx.Request) -> httpx.Response:
        modes = json.loads(request.content)["variables"]["modes"]
        key = ",".join(m["mode"] for m in modes)
        return by_key.get(key, httpx.Response(200, json=_gql([])))

    return handler
