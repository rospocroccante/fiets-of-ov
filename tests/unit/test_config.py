"""Settings guards: the checks that stop a misconfigured process from starting.

Two of them, both about failing loudly instead of quietly doing the wrong thing:

- **JWT secret.** The dev placeholder is public in this repository, so anything that isn't
  a developer sandbox (`APP_ENV` other than dev/test) must refuse to build settings with it,
  or with a secret too short for HS256.
- **CORS allowlist.** `*` is rejected outright, because the middleware advertises credential
  support and a wildcard would make it reflect any caller's origin.

`Settings` is constructed directly with keyword arguments rather than through
`get_settings()`: init kwargs outrank both the environment and `.env` in pydantic-settings,
so each case is hermetic and nothing has to be monkeypatched or cache-cleared.
"""

import pytest
from pydantic import ValidationError

from app.core.config import DEV_JWT_SECRET, MIN_JWT_SECRET_BYTES, Settings

STRONG_SECRET = "f" * MIN_JWT_SECRET_BYTES


def test_defaults_are_a_working_dev_sandbox():
    # The whole point of the dev default: `make run` and the offline test suite need no
    # configuration at all, placeholder secret included.
    settings = Settings()

    assert settings.app_env == "dev"
    assert settings.jwt_secret == DEV_JWT_SECRET
    assert settings.cors_allow_origins == ["http://localhost:5173"]


@pytest.mark.parametrize("app_env", ["dev", "test", "DEV", " test "])
def test_placeholder_secret_is_allowed_in_dev_and_test(app_env):
    # Case and stray whitespace must not accidentally arm the guard on a developer's machine.
    assert Settings(app_env=app_env, jwt_secret=DEV_JWT_SECRET).jwt_secret == DEV_JWT_SECRET


def test_placeholder_secret_is_refused_outside_dev():
    with pytest.raises(ValidationError, match="development placeholder"):
        Settings(app_env="prod", jwt_secret=DEV_JWT_SECRET)


def test_short_secret_is_refused_outside_dev():
    # Overridden, but with something an attacker could brute-force — the guard is about key
    # strength, not just about the literal placeholder string.
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(app_env="prod", jwt_secret="short")


def test_secret_length_is_measured_in_bytes_not_characters():
    # What HS256 consumes is key *material*, so the floor counts UTF-8 bytes. A 16-character
    # secret of 2-byte characters carries exactly 32 bytes and is fine; the same 16
    # characters in ASCII would not be. Counting len() would get one of those two wrong.
    assert len("é" * 16) == 16 and len("é".encode()) == 2

    Settings(app_env="prod", jwt_secret="é" * 16)  # 32 bytes -> accepted
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(app_env="prod", jwt_secret="a" * 16)  # 16 bytes -> refused


def test_unknown_environment_is_treated_as_production():
    # Fail-safe: a typo'd or new APP_ENV must arm the guard, never silently disable it.
    with pytest.raises(ValidationError, match="development placeholder"):
        Settings(app_env="staging", jwt_secret=DEV_JWT_SECRET)


def test_strong_secret_outside_dev_is_accepted():
    assert Settings(app_env="prod", jwt_secret=STRONG_SECRET).jwt_secret == STRONG_SECRET


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The form people actually type into a compose file or a systemd unit...
        ("http://a.test,http://b.test", ["http://a.test", "http://b.test"]),
        ("http://a.test, http://b.test ", ["http://a.test", "http://b.test"]),
        ("http://a.test", ["http://a.test"]),
        # ...and the JSON array pydantic-settings documents, which must keep working.
        ('["http://a.test", "http://b.test"]', ["http://a.test", "http://b.test"]),
    ],
)
def test_cors_origins_parse_from_the_environment(monkeypatch, raw, expected):
    # Read through the env var, not an init kwarg: the env source is where pydantic-settings
    # would normally JSON-decode a list field, so this is the path the NoDecode opt-out and
    # `_parse_origin_list` exist to control.
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", raw)

    assert Settings().cors_allow_origins == expected


@pytest.mark.parametrize("origins", ["*", "http://a.test,*"])
def test_wildcard_cors_origin_is_refused(origins):
    # Rejected in every environment, alone or smuggled into a longer list: the middleware
    # sends credentials-capable CORS, so a wildcard is an origin-reflection hole.
    with pytest.raises(ValidationError, match="never '\\*'"):
        Settings(cors_allow_origins=origins)
