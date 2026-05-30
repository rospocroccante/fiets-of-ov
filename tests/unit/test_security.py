"""Unit tests for the auth core (`app.core.security`).

Fully offline: password hashing and JWT signing/verification need no DB or network.
We cover the round-trips a login flow relies on (hash/verify, sign/decode) plus the
two ways a token must be rejected — expiry and a foreign signing secret.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def _future_exp() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


def test_hash_verify_roundtrip():
    # A freshly hashed password verifies against its own hash. The hash must not equal
    # the plaintext (i.e. we are actually hashing, not storing it raw).
    hashed = hash_password("correct horse battery staple")

    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_rejects_wrong_password():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("Tr0ub4dour&3", hashed) is False


def test_create_then_decode_returns_subject():
    # The whole point of the token: round-trip an identity (the user id) through a signed
    # JWT and get it back intact.
    token = create_access_token("42")

    assert decode_token(token) == "42"


def test_expired_token_raises():
    # A negative lifetime backdates `exp`, so the token is already expired at decode time;
    # PyJWT must reject it and we surface that as our domain TokenError.
    token = create_access_token("42", expires_delta=timedelta(minutes=-1))

    with pytest.raises(TokenError):
        decode_token(token)


def test_token_signed_with_other_secret_raises(monkeypatch):
    # Sign a token, then change the server secret so the stored signature no longer
    # verifies — a tampered/foreign token must be rejected, not silently accepted.
    token = create_access_token("42")

    get_settings.cache_clear()
    # Use a >=32-byte foreign secret so the rejection is about the signature, not PyJWT's
    # short-key warning (which would otherwise fire now that we no longer suppress it).
    monkeypatch.setenv("JWT_SECRET", "a-different-secret-at-least-32-bytes-long")
    try:
        with pytest.raises(TokenError):
            decode_token(token)
    finally:
        # Drop the per-test settings so the swapped secret can't leak into other tests.
        get_settings.cache_clear()


def test_alg_none_token_is_rejected():
    # The classic JWT downgrade: an unsigned "alg: none" token must never be accepted,
    # because decode pins the allowed algorithms to HS256.
    forged = jwt.encode({"sub": "42", "exp": _future_exp()}, "", algorithm="none")

    with pytest.raises(TokenError):
        decode_token(forged)


def test_token_with_non_allowed_algorithm_is_rejected():
    # A token signed HS512 must be rejected: decode only accepts HS256, closing
    # algorithm-confusion. (The signing key is long enough to clear HS512's own key-length
    # floor — decode rejects on the algorithm regardless of the key.)
    forged = jwt.encode({"sub": "42", "exp": _future_exp()}, "k" * 64, algorithm="HS512")

    with pytest.raises(TokenError):
        decode_token(forged)


def test_token_without_exp_is_rejected():
    # Validly signed but missing `exp` — must be rejected so every accepted token expires.
    secret = get_settings().jwt_secret
    forged = jwt.encode({"sub": "42"}, secret, algorithm="HS256")

    with pytest.raises(TokenError):
        decode_token(forged)


def test_token_without_subject_is_rejected():
    secret = get_settings().jwt_secret
    forged = jwt.encode({"exp": _future_exp()}, secret, algorithm="HS256")

    with pytest.raises(TokenError):
        decode_token(forged)
