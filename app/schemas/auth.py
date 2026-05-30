"""Request/response schemas for user registration and JWT auth.

These are the public contract of the `/v1/auth` endpoints. `UserCreate` is the
registration body, `UserOut` is the safe view of a user (never exposes the password
hash), and `Token` is the bearer token handed back by the token endpoint.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    """Registration body: a valid email plus a password.

    `EmailStr` rejects malformed addresses before any DB work. Email is normalised to a
    canonical lowercased form so a single human can't end up with two case-variant
    accounts (and so login matches regardless of the case the user types). The password
    is bounded to bcrypt's 72-byte limit — bcrypt 5.x *raises* on longer input rather than
    truncating, so an unbounded password would otherwise 500 the register endpoint.
    """

    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        # EmailStr lowercases only the domain; lowercase the whole address so the unique
        # index and the login lookup are effectively case-insensitive.
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _within_bcrypt_limit(cls, value: str) -> str:
        # bcrypt hashes at most 72 bytes; bcrypt 5.x raises beyond that. Reject here with a
        # clear 422 instead of letting it crash the endpoint, and avoid silent truncation
        # (two long passwords sharing a 72-byte prefix must not authenticate as one).
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 bytes")
        return value


class UserOut(BaseModel):
    """Safe view of a user returned to clients.

    Deliberately omits `hashed_password` so the credential never leaves the service.
    `from_attributes` lets FastAPI serialize a SQLAlchemy `User` row directly.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    created_at: datetime


class Token(BaseModel):
    """OAuth2 bearer token response from `POST /v1/auth/token`."""

    access_token: str
    # OAuth2 password flow mandates this literal; clients send `Authorization: Bearer <token>`.
    token_type: str = "bearer"
