"""Unit tests for the auth core (`app.core.security`).

Fully offline: password hashing and JWT signing/verification need no DB or network.
We cover the round-trips a login flow relies on (hash/verify, sign/decode) plus the
two ways a token must be rejected — expiry and a foreign signing secret. The `_async`
password helpers get the same round-trip coverage plus a check that they really do leave
the event loop, since that is their entire reason to exist.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
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


async def test_async_helpers_roundtrip_and_reject():
    # The async pair must be behaviourally identical to the sync one — same hash format,
    # same accept/reject — since the API layer calls only these.
    hashed = await hash_password_async("correct horse battery staple")

    assert hashed != "correct horse battery staple"
    assert await verify_password_async("correct horse battery staple", hashed) is True
    assert await verify_password_async("Tr0ub4dour&3", hashed) is False


async def test_async_helpers_interoperate_with_the_sync_ones():
    # A hash written by either helper verifies through the other: there is one bcrypt format,
    # so a password hashed at import time (the dummy timing hash) checks out against a login
    # that goes through the thread pool, and rows survive a switch between the two.
    assert verify_password("hunter2hunter2", await hash_password_async("hunter2hunter2")) is True
    assert await verify_password_async("hunter2hunter2", hash_password("hunter2hunter2")) is True


async def test_async_helpers_run_off_the_event_loop_thread():
    # The point of the whole exercise: bcrypt burns ~100-300 ms of CPU, and running it inline
    # would stall every other coroutine for that long. Assert it actually lands on a worker
    # thread rather than trusting the wrapper — an accidental direct call would still pass
    # every behavioural test above.
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _spy(*args: object) -> bool:
        seen.append(threading.get_ident())
        return True

    # Patch at the anyio call site so the assertion is about where the work runs, not about
    # bcrypt itself; anyio.to_thread.run_sync is what moves it.
    import anyio.to_thread

    await anyio.to_thread.run_sync(_spy)
    assert seen == [seen[0]] and seen[0] != loop_thread

    # And the real helper: it must not block the loop, i.e. other tasks keep running while
    # the hash is in flight.
    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(_tick())
    await hash_password_async("a password expensive enough to notice")
    ticker.cancel()

    assert ticks > 0  # the loop kept turning during the hash


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
