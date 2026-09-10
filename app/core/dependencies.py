import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import UUID

from app.core.security import JWTError, decode_access_token, hash_token
from app.db.models import Kiosk, KioskStatus, User
from app.db.session import AsyncSessionLocal

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        token_data = decode_access_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")

    try:
        user_id = UUID(token_data.sub)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

    statement = select(User).where(User.id == user_id)
    result = await session.execute(statement)
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(optional_security),
    session: AsyncSession = Depends(get_session),
) -> Optional[User]:
    if not credentials or not credentials.credentials:
        return None
    try:
        token_data = decode_access_token(credentials.credentials)
    except JWTError:
        return None
    try:
        user_id = UUID(token_data.sub)
    except ValueError:
        return None
    statement = select(User).where(User.id == user_id)
    result = await session.execute(statement)
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user


async def get_current_kiosk(
    x_kiosk_token: Optional[str] = Header(None, alias="X-Kiosk-Token"),
    session: AsyncSession = Depends(get_session),
) -> Kiosk:
    """A kiosk is a device, not a person — a separate opaque-token scheme on
    its own header, not the JWT-bearer path `get_current_user` uses. A device
    left in a corridor is assumed compromised, so the token is looked up by
    hash the same way a refresh token is, and every use bumps `last_seen_at`
    so a revoked-but-still-calling device is visible.
    """
    if not x_kiosk_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing kiosk token")

    statement = select(Kiosk).where(Kiosk.device_token_hash == hash_token(x_kiosk_token))
    result = await session.execute(statement)
    kiosk = result.scalar_one_or_none()
    if not kiosk or kiosk.status != KioskStatus.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked kiosk token")

    kiosk.last_seen_at = datetime.utcnow()
    session.add(kiosk)
    await session.commit()
    await session.refresh(kiosk)
    return kiosk


_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)


def rate_limiter(max_requests: int, window_seconds: int):
    """Per-IP sliding-window limiter for anonymous, high-value endpoints (the
    invitation token is the only secret protecting `GET/POST /invitations/{token}`
    — see docs/SPEC_ACCOUNT_PROVISIONING.md §3.4).

    In-memory rather than Redis-backed: this process has no other shared cache,
    and a per-instance limit is still a real speed bump against token guessing,
    even though it resets on restart and doesn't span multiple instances.
    """

    async def _check(request: Request) -> None:
        key = f"{request.url.path}:{request.client.host if request.client else 'unknown'}"
        now = time.monotonic()
        bucket = _rate_limit_buckets[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= max_requests:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests. Try again shortly.")
        bucket.append(now)

    return _check
