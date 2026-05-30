"""`/v1/auth` — user registration and OAuth2 token issuance (Phase 5).

Thin controller, per project convention: it validates the body, talks to the database
through the request-scoped session, and delegates all password/JWT mechanics to
`app/core/security.py`. No hashing or signing logic lives here.

Two endpoints:
- `POST /v1/auth/register` creates an account (409 on a duplicate email).
- `POST /v1/auth/token` exchanges email + password for a short-lived bearer token,
  using the standard OAuth2 password form so it plugs into the docs "Authorize" flow.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_session
from app.models.user import User
from app.schemas.auth import Token, UserCreate, UserOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# A real bcrypt hash that no real password matches, used to equalise login timing: when the
# email is unknown we still run a verify against this so an attacker can't distinguish
# "no such account" from "wrong password" by how long the request takes.
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-timing-equaliser")


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserCreate,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Create a new account and return its safe view (`UserOut`, no password hash).

    The password is bcrypt-hashed before it ever reaches the database. A duplicate email
    is a 409: the unique index on `users.email` is the source of truth, so we let the
    insert hit it and translate the `IntegrityError` rather than racing a prior SELECT —
    that closes the check-then-insert window two concurrent registrations could exploit.
    """
    user = User(email=body.email, hashed_password=hash_password(body.password))
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The only unique constraint on the table is ix_users_email, so a violation here
        # can only mean the address is already taken.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a user with this email already exists",
        ) from exc
    await session.commit()
    # Re-read isn't needed: server-default columns (id, created_at) are populated on flush
    # and expire_on_commit=False keeps the instance usable after commit.
    return user


@router.post("/token", response_model=Token)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> Token:
    """Verify credentials and issue an access token (OAuth2 password flow).

    The OAuth2 form carries the email in its `username` field. On unknown email *or* a bad
    password we return the same opaque 401, so the response never reveals whether an email
    is registered. The token's subject is the user id as a string, which `get_current_user`
    reads back to load the account.
    """
    # Emails are stored canonically lowercased (see UserCreate), so match the same way —
    # otherwise a user who registered as "Alice@x" could never log in via "alice@x".
    email = form.username.strip().lower()
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if user is None:
        # No such account. Still spend a bcrypt verify against a dummy hash so the timing
        # is indistinguishable from a wrong-password attempt (no user-enumeration oracle).
        verify_password(form.password, _DUMMY_PASSWORD_HASH)
        raise invalid_credentials
    if not verify_password(form.password, user.hashed_password):
        raise invalid_credentials

    return Token(access_token=create_access_token(subject=str(user.id)))
