"""Integration tests for `/v1/auth` against a real, migrated Postgres.

Exercises the registration + token flow end-to-end through the ASGI app: a fresh account
registers (201), a duplicate email is rejected (409), valid credentials mint a bearer token
(200), and a wrong password is refused (401). We drive HTTP with httpx's ASGITransport (no
sockets) and swap the request-scoped DB session for the test's rolled-back one via
`app.dependency_overrides`, so nothing the test writes is ever persisted.
"""

from httpx import ASGITransport, AsyncClient

from app.db.session import get_session
from app.main import app


def _client(session) -> AsyncClient:
    """Build an in-process ASGI client whose DB session is the test's rolled-back one.

    Overriding `get_session` to yield the very session the test owns means the app's writes
    land in the same outer transaction the conftest rolls back — so registration here never
    leaks a row into the (possibly shared) target database.
    """

    async def _use_test_session():
        yield session

    app.dependency_overrides[get_session] = _use_test_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_register_returns_created_user(session):
    try:
        async with _client(session) as client:
            response = await client.post(
                "/v1/auth/register",
                json={"email": "alice@example.com", "password": "supersecret"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert "id" in body and isinstance(body["id"], int)
    assert "created_at" in body
    # The safe view must never echo the credential back to the client.
    assert "password" not in body
    assert "hashed_password" not in body


async def test_register_duplicate_email_conflicts(session):
    try:
        async with _client(session) as client:
            first = await client.post(
                "/v1/auth/register",
                json={"email": "dup@example.com", "password": "supersecret"},
            )
            # Same address again: the unique index on users.email must turn this into a 409.
            second = await client.post(
                "/v1/auth/register",
                json={"email": "dup@example.com", "password": "anothersecret"},
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201
    assert second.status_code == 409


async def test_token_with_valid_credentials_returns_bearer(session):
    try:
        async with _client(session) as client:
            await client.post(
                "/v1/auth/register",
                json={"email": "bob@example.com", "password": "hunter2hunter2"},
            )
            # The token endpoint speaks the OAuth2 password form: email goes in `username`.
            response = await client.post(
                "/v1/auth/token",
                data={"username": "bob@example.com", "password": "hunter2hunter2"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]


async def test_token_with_wrong_password_unauthorized(session):
    try:
        async with _client(session) as client:
            await client.post(
                "/v1/auth/register",
                json={"email": "carol@example.com", "password": "rightpassword"},
            )
            response = await client.post(
                "/v1/auth/token",
                data={"username": "carol@example.com", "password": "wrongpassword"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


async def test_email_is_case_insensitive(session):
    # Registering a case-variant of an existing email is the same account (409), and login
    # works regardless of the case typed — otherwise one human gets two confusable accounts.
    try:
        async with _client(session) as client:
            first = await client.post(
                "/v1/auth/register",
                json={"email": "Mixed.Case@Example.com", "password": "supersecret"},
            )
            dup = await client.post(
                "/v1/auth/register",
                json={"email": "mixed.case@example.com", "password": "supersecret"},
            )
            # Log in with yet another casing of the same address.
            token = await client.post(
                "/v1/auth/token",
                data={"username": "MIXED.CASE@EXAMPLE.COM", "password": "supersecret"},
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201
    assert first.json()["email"] == "mixed.case@example.com"  # stored canonical lowercase
    assert dup.status_code == 409  # same account, not a second one
    assert token.status_code == 200  # login is case-insensitive


async def test_register_overlong_password_is_422_not_500(session):
    # bcrypt only hashes 72 bytes and bcrypt 5.x raises beyond that; the schema must reject
    # an over-long password as a 422, never let it crash the endpoint.
    try:
        async with _client(session) as client:
            response = await client.post(
                "/v1/auth/register",
                json={"email": "long@example.com", "password": "x" * 100},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
