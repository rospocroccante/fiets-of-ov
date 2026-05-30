"""Password hashing and JWT access-token helpers (Phase 5 auth core).

This module is deliberately **pure and offline**: it never touches the database or the
network, so it can be unit-tested with fixtures alone. The two concerns it owns are:

- *Passwords* — hashed with bcrypt, which bundles a per-hash salt into its output, so
  `verify_password` re-derives the salt from the stored hash (no separate salt column).
  bcrypt only hashes the first 72 bytes and bcrypt 5.x **raises** on longer input, so the
  registration schema (`UserCreate`) rejects over-72-byte passwords up front (422) — we
  neither crash nor silently truncate. A SHA prehash to lift the cap is out of scope.

- *Access tokens* — short-lived HS256 JWTs whose subject (`sub`) is the user id. HS256 is
  symmetric, which suits a single service: the same `jwt_secret` signs and verifies, with
  no public-key distribution. We embed `exp`/`iat` as timezone-aware UTC instants so token
  expiry is unambiguous regardless of server local time.
"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings


class TokenError(Exception):
    """Raised when an access token cannot be trusted (expired, tampered, or malformed).

    Callers (the auth dependency) map this to a 401 — we surface a single domain error
    rather than leaking PyJWT's exception hierarchy into the API layer.
    """


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt, returning the encoded hash as a str.

    The returned string embeds the algorithm, cost and salt, so it is the only value
    that needs persisting; `verify_password` reads everything it needs back out of it.
    The caller (UserCreate) bounds the password to 72 bytes, so bcrypt never raises here.
    """
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Return True iff `password` matches the previously stored bcrypt `hashed`.

    Returns False (never raises) on any mismatch, including a malformed/corrupt stored
    hash. Swallowing the error keeps the login path uniform — an unparseable hash is just
    a failed authentication, not a 500.
    """
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        # bcrypt raises if `hashed` is not a valid bcrypt string. Treat that as "no match"
        # so a bad row can never crash authentication.
        return False


def create_access_token(subject: str, expires_delta: timedelta | None = None) -> str:
    """Sign and return an HS256 access token whose `sub` claim is `subject`.

    Args:
        subject: the token's principal — here the user id as a string.
        expires_delta: optional lifetime override; defaults to the configured
            `access_token_expire_minutes`.

    Returns the encoded JWT string. `exp` and `iat` are timezone-aware UTC instants.
    """
    settings = get_settings()
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    # Anchor on UTC-aware "now" so exp/iat are unambiguous and PyJWT compares them
    # correctly against its own UTC clock at decode time.
    now = datetime.now(UTC)
    payload = {"sub": subject, "exp": now + delta, "iat": now}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str:
    """Verify `token` and return its subject (`sub`).

    Raises `TokenError` if the token is expired, has a bad signature, is otherwise
    malformed, or lacks a `sub` claim. Verifying the signature with our own secret is what
    makes the returned subject trustworthy as an identity.
    """
    settings = get_settings()
    try:
        # PyJWT verifies the signature and the `exp` claim for us. require=[exp,sub] also
        # rejects a validly-signed token that simply omits exp — so every accepted token is
        # guaranteed to expire (our "short-lived tokens" model holds even for forged ones).
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Collapse the whole PyJWT error hierarchy (expired, bad signature, malformed,
        # wrong algorithm, ...) into our single domain error for the API layer.
        raise TokenError("could not validate token") from exc

    subject = payload.get("sub")
    if not subject:
        # A signed-but-subjectless token is structurally valid yet useless — there is no
        # identity to act on, so treat it as untrustworthy.
        raise TokenError("token missing subject")
    return subject
