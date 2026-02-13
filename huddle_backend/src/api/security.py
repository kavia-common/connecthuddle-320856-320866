import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET env var is required")
    return secret


def _jwt_algorithm() -> str:
    return os.getenv("JWT_ALGORITHM", "HS256")


def _jwt_expires_minutes() -> int:
    try:
        return int(os.getenv("JWT_EXPIRES_MINUTES", "10080"))
    except ValueError:
        return 10080


# PUBLIC_INTERFACE
def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return _pwd_context.hash(password)


# PUBLIC_INTERFACE
def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    return _pwd_context.verify(password, password_hash)


# PUBLIC_INTERFACE
def create_access_token(subject: str, extra_claims: Optional[Dict[str, Any]] = None) -> str:
    """Create a signed JWT access token for a user id (subject)."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=_jwt_expires_minutes())
    payload: Dict[str, Any] = {"sub": subject, "iat": int(now.timestamp()), "exp": exp}
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _jwt_secret(), algorithm=_jwt_algorithm())


# PUBLIC_INTERFACE
def decode_token(token: str) -> Dict[str, Any]:
    """Decode and validate a JWT token; raises HTTPException on failure."""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[_jwt_algorithm()])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


# PUBLIC_INTERFACE
async def get_current_user_id(token: str = Depends(oauth2_scheme)) -> str:
    """FastAPI dependency to extract authenticated user id from Authorization: Bearer token."""
    payload = decode_token(token)
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    return sub
