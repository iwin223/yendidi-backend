import time
from collections import defaultdict
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.security import JWTError, decode_access_token
from app.db.models import User
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
