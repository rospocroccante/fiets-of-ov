"""Integration tests for `/v1/trip-alerts` against a real, migrated Postgres.

Covers the auth gate (401 without a token), creation with the geocoder stubbed (the stored
coordinates must come from the resolver, not the body), owner-scoped listing, and the
ownership rules on delete: a user can remove their own trip alert but a second user can
neither delete nor see it.

We drive HTTP in-process with httpx's ASGITransport and override two dependencies via
`app.dependency_overrides`: `get_session` (so writes land in the test's rolled-back
transaction) and `get_geocoder_client` (so no name lookup ever hits the network). The stub
geocoder returns fixed coordinates, which lets us assert the resolver — not the request body
— is what fills `origin_lat`/`origin_lon`.
"""

from datetime import timedelta

import httpx
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_geocoder_client
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app

# Fixed coordinates the stub geocoder returns for any place name. They are deliberately not
# in the request body, so finding them on the stored row proves the resolver supplied them.
ORIGIN_COORDS = (52.3791, 4.9003)
DEST_COORDS = (52.3376, 4.8852)


class _StubGeocoder:
    """A geocoder that resolves the origin to one fixed point and everything else to another.

    No network: it returns canned coordinates by name so trip-alert creation is hermetic.
    Distinguishing the origin from the destination lets the test assert each endpoint's
    coordinates were resolved independently (origin → ORIGIN_COORDS, dest → DEST_COORDS).
    """

    def __init__(self, origin_name: str) -> None:
        self._origin_name = origin_name

    async def geocode(self, query: str) -> tuple[float, float]:
        return ORIGIN_COORDS if query == self._origin_name else DEST_COORDS


def _transport_client() -> AsyncClient:
    """An in-process ASGI client (no sockets)."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _override_session(session) -> None:
    """Point the app's `get_session` dependency at the test's rolled-back session.

    The override must itself be an async-generator function (not a lambda returning one) so
    FastAPI drives it as a generator dependency — otherwise the endpoint receives the raw
    generator object instead of an `AsyncSession`.
    """

    async def _use_test_session():
        yield session

    app.dependency_overrides[get_session] = _use_test_session


async def _register_and_token(client: AsyncClient, email: str, password: str) -> str:
    """Register an account and return a bearer token for it.

    Used to set up the authenticated callers each test needs; returns the raw token so the
    test can build the `Authorization: Bearer` header itself.
    """
    register = await client.post("/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code == 201
    token = await client.post("/v1/auth/token", data={"username": email, "password": password})
    assert token.status_code == 200
    return token.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    """Build the bearer Authorization header for a token."""
    return {"Authorization": f"Bearer {token}"}


# A complete, valid creation body. `origin`/`destination` are names (not "lat,lon"), so the
# stub geocoder — not a coordinate parse — is what resolves them.
_ALERT_BODY = {
    "label": "commute",
    "origin": "Amsterdam Centraal",
    "destination": "Rijksmuseum",
    "departure_time": "08:30:00",
    "days": [1, 2, 3, 4, 5],
}


async def test_trip_alerts_require_a_token(session):
    # No Authorization header → the get_current_user dependency must reject the request.
    _override_session(session)
    try:
        async with _transport_client() as client:
            response = await client.get("/v1/trip-alerts")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


async def test_create_uses_resolver_coordinates(session):
    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _StubGeocoder(_ALERT_BODY["origin"])
    try:
        async with _transport_client() as client:
            token = await _register_and_token(client, "owner@example.com", "supersecret")
            response = await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(token))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "commute"
    # The coordinates must be the resolver's, not anything from the request body.
    assert (body["origin_lat"], body["origin_lon"]) == ORIGIN_COORDS
    assert (body["dest_lat"], body["dest_lon"]) == DEST_COORDS
    assert body["days"] == [1, 2, 3, 4, 5]
    assert "id" in body and isinstance(body["id"], int)


async def test_list_returns_only_callers_trip_alerts(session):
    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _StubGeocoder(_ALERT_BODY["origin"])
    try:
        async with _transport_client() as client:
            alice = await _register_and_token(client, "alice2@example.com", "supersecret")
            bob = await _register_and_token(client, "bob2@example.com", "supersecret")

            # Alice creates two trip alerts; Bob creates one.
            await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(alice))
            second = {**_ALERT_BODY, "label": "evening"}
            await client.post("/v1/trip-alerts", json=second, headers=_auth(alice))
            await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(bob))

            alice_list = await client.get("/v1/trip-alerts", headers=_auth(alice))
            bob_list = await client.get("/v1/trip-alerts", headers=_auth(bob))
    finally:
        app.dependency_overrides.clear()

    assert alice_list.status_code == 200
    alice_alerts = alice_list.json()
    assert len(alice_alerts) == 2
    # Newest first: the "evening" alert was created last, so it leads.
    assert alice_alerts[0]["label"] == "evening"
    assert alice_alerts[1]["label"] == "commute"

    bob_alerts = bob_list.json()
    assert len(bob_alerts) == 1  # Bob sees only his own row, not Alice's two


async def test_delete_own_trip_alert_then_gone(session):
    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _StubGeocoder(_ALERT_BODY["origin"])
    try:
        async with _transport_client() as client:
            token = await _register_and_token(client, "deleter@example.com", "supersecret")
            created = await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(token))
            alert_id = created.json()["id"]

            deleted = await client.delete(f"/v1/trip-alerts/{alert_id}", headers=_auth(token))
            # A second delete of the same id must now 404 — the row is genuinely gone.
            again = await client.delete(f"/v1/trip-alerts/{alert_id}", headers=_auth(token))
            listing = await client.get("/v1/trip-alerts", headers=_auth(token))
    finally:
        app.dependency_overrides.clear()

    assert deleted.status_code == 204
    assert again.status_code == 404
    assert listing.json() == []


async def test_other_user_cannot_delete_or_see_trip_alert(session):
    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _StubGeocoder(_ALERT_BODY["origin"])
    try:
        async with _transport_client() as client:
            owner = await _register_and_token(client, "first@example.com", "supersecret")
            intruder = await _register_and_token(client, "second@example.com", "supersecret")

            created = await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(owner))
            alert_id = created.json()["id"]

            # The intruder must not be able to delete a row they don't own — same 404 as if
            # it didn't exist, so the API never confirms another user's trip alert exists.
            intruder_delete = await client.delete(
                f"/v1/trip-alerts/{alert_id}", headers=_auth(intruder)
            )
            intruder_list = await client.get("/v1/trip-alerts", headers=_auth(intruder))
            # And the owner's row is untouched by the failed cross-user delete.
            owner_list = await client.get("/v1/trip-alerts", headers=_auth(owner))
    finally:
        app.dependency_overrides.clear()

    assert intruder_delete.status_code == 404
    assert intruder_list.json() == []  # the intruder never sees the owner's trip alert
    assert len(owner_list.json()) == 1  # still there


async def test_expired_token_is_rejected(session):
    # A bearer token past its expiry must 401 at the auth dependency, before any DB work.
    _override_session(session)
    try:
        async with _transport_client() as client:
            expired = create_access_token("1", expires_delta=timedelta(minutes=-1))
            response = await client.get("/v1/trip-alerts", headers=_auth(expired))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


async def test_malformed_token_is_rejected(session):
    _override_session(session)
    try:
        async with _transport_client() as client:
            response = await client.get(
                "/v1/trip-alerts", headers={"Authorization": "Bearer not.a.jwt"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


async def test_delete_out_of_range_id_is_422(session):
    # An id beyond Postgres int4 must be rejected by the path validator (422), not reach the
    # query and surface asyncpg's "out of int32 range" as a 500.
    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _StubGeocoder(_ALERT_BODY["origin"])
    try:
        async with _transport_client() as client:
            token = await _register_and_token(client, "hugeid@example.com", "supersecret")
            response = await client.delete("/v1/trip-alerts/9999999999", headers=_auth(token))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


async def test_create_with_geocoder_outage_is_502(session):
    class _BrokenGeocoder:
        """Geocoder whose upstream is down (raises httpx.HTTPError)."""

        async def geocode(self, query: str) -> tuple[float, float]:
            raise httpx.ConnectError("nominatim down")

    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _BrokenGeocoder()
    try:
        async with _transport_client() as client:
            token = await _register_and_token(client, "outage@example.com", "supersecret")
            response = await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(token))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502


async def test_create_with_unresolvable_place_is_400(session):
    class _NotFoundGeocoder:
        """Geocoder that always reports the name as unresolvable (domain GeocodeNotFound)."""

        async def geocode(self, query: str) -> tuple[float, float]:
            from app.clients.geocoder import GeocodeNotFound

            raise GeocodeNotFound(query)

    _override_session(session)
    app.dependency_overrides[get_geocoder_client] = lambda: _NotFoundGeocoder()
    try:
        async with _transport_client() as client:
            token = await _register_and_token(client, "badplace@example.com", "supersecret")
            response = await client.post("/v1/trip-alerts", json=_ALERT_BODY, headers=_auth(token))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
