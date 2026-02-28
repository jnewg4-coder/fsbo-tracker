"""FSBO Tracker — Authentication service (JWT + bcrypt).

Forked from AVMLens auth_service.py, adapted for FSBO standalone DB.
"""

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Tuple, Optional

import bcrypt
import jwt
from fastapi import HTTPException, status

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    if ENVIRONMENT == "production":
        raise RuntimeError("JWT_SECRET must be set in production")
    JWT_SECRET = "dev-insecure-" + secrets.token_hex(32)
    print("[AUTH] WARNING: Using auto-generated JWT_SECRET — set JWT_SECRET env var in production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

# Brute-force protection
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(
    user_id: str,
    email: str,
    role: str = "user",
    tier: str = "free",
    token_version: int = 0,
) -> Tuple[str, int]:
    """Create JWT. Returns (token, expires_in_seconds).

    Includes tier and token_version in claims so stale JWTs
    (after tier change) can be detected without a DB lookup on every request.
    """
    expires_at = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "tier": tier,
        "tv": token_version,  # compact claim name for token version
        "exp": expires_at,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, JWT_EXPIRE_HOURS * 3600


def decode_token(token: str) -> dict:
    """Decode and validate JWT. Raises HTTPException if invalid."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def generate_verification_code() -> str:
    """Generate 6-digit verification code."""
    return str(secrets.randbelow(900000) + 100000)


def generate_reset_token() -> str:
    """Generate secure password reset token."""
    return secrets.token_urlsafe(32)
