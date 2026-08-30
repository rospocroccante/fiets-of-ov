"""CORS: the browser frontend lives on another origin, so the API has to say so explicitly.

Exercised against `/health` — the endpoint with no dependencies — because what is under test
is the middleware, not any route. The allowlist comes from settings and is read once at
import (`app/main.py`), so these assertions run against the default: the Vite dev server on
:5173, and nothing else.

The security-relevant assertion is the negative one: an origin outside the allowlist must
come back with *no* `access-control-allow-origin` header at all. A response that quietly
echoes the caller's origin, which is what a `*` allowlist plus credentials would produce, is
the failure mode this whole configuration exists to prevent.
"""

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

client = TestClient(app)

ALLOWED = "http://localhost:5173"
DISALLOWED = "http://evil.test"


def test_allowed_origin_gets_cors_headers():
    response = client.get("/health", headers={"Origin": ALLOWED})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["access-control-allow-credentials"] == "true"


def test_disallowed_origin_gets_no_cors_headers():
    response = client.get("/health", headers={"Origin": DISALLOWED})

    # The request itself still succeeds — CORS is enforced by the browser, not the server —
    # but without the header the browser refuses to hand the body to the calling page.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_from_an_allowed_origin_is_answered():
    # The preflight a browser sends before an authenticated POST. It must be answered with
    # the exact origin and must list the Authorization header, or the real request never
    # leaves the browser.
    response = client.options(
        "/v1/auth/token",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_preflight_from_a_disallowed_origin_is_refused():
    response = client.options(
        "/v1/auth/token",
        headers={"Origin": DISALLOWED, "Access-Control-Request-Method": "POST"},
    )

    assert response.headers.get("access-control-allow-origin") != DISALLOWED


def test_configured_allowlist_is_never_a_wildcard():
    # Belt and braces over the settings-level guard: with credentials enabled, a wildcard
    # would make Starlette reflect any caller's origin, so it must be impossible to reach
    # that state through configuration.
    assert "*" not in get_settings().cors_allow_origins
