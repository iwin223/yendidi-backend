from datetime import datetime, timedelta
import hashlib
import secrets
from typing import Any, Optional
from uuid import uuid4

from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from app.core.config import settings

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


class TokenData(BaseModel):
    sub: str
    role: str
    exp: Optional[int] = None
    jti: Optional[str] = None
    school_id: Optional[str] = None
    profile_id: Optional[str] = None
    device_id: Optional[str] = None
    mfa_verified: bool = False


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def _get_jwt_signing_key() -> str:
    if settings.jwt_algorithm.upper().startswith("RS"):
        if not settings.jwt_private_key:
            raise RuntimeError("JWT_PRIVATE_KEY is required for RS256")
        return settings.jwt_private_key
    return settings.jwt_secret


def _get_jwt_verifying_key() -> str:
    if settings.jwt_algorithm.upper().startswith("RS"):
        if not settings.jwt_public_key:
            raise RuntimeError("JWT_PUBLIC_KEY is required for RS256")
        return settings.jwt_public_key
    return settings.jwt_secret


def create_access_token(
    subject: str,
    role: str,
    expires_delta: Optional[timedelta] = None,
    device_id: Optional[str] = None,
    school_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    mfa_verified: bool = False,
    additional_claims: Optional[dict[str, Any]] = None,
) -> str:
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "jti": str(uuid4()),
        "device_id": device_id,
        "school_id": school_id,
        "profile_id": profile_id,
        "mfa_verified": mfa_verified,
    }
    if additional_claims:
        to_encode.update(additional_claims)
    return jwt.encode(to_encode, _get_jwt_signing_key(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenData:
    payload = jwt.decode(token, _get_jwt_verifying_key(), algorithms=[settings.jwt_algorithm])
    return TokenData(**payload)


def create_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_refresh_token(token: str, token_hash: str) -> bool:
    return hash_token(token) == token_hash
