"""FSBO Tracker — Auth API endpoints (signup, login, me).

Phase 1: email/password auth + JWT.
Phase 2: Google OAuth + Helcim payments.
"""

import hmac
import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr

from .auth_service import decode_token
from . import auth_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
security = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ---------------------------------------------------------------------------
# JWT dependency (replaces X-Admin-Password for protected endpoints)
# ---------------------------------------------------------------------------

# In-memory cache for user existence checks (60s TTL — short to limit deactivation window)
_user_cache: dict = {}
_CACHE_TTL = 60


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Validate JWT and return user payload."""
    if credentials:
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")

        now = time.time()
        if user_id in _user_cache:
            cached_at, exists = _user_cache[user_id]
            if now - cached_at < _CACHE_TTL and exists:
                return payload

        if user_id and not auth_db.user_exists(user_id):
            _user_cache[user_id] = (now, False)
            raise HTTPException(status_code=401, detail="Account no longer active")

        _user_cache[user_id] = (now, True)
        return payload

    raise HTTPException(status_code=401, detail="Authentication required")


async def get_current_user_or_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Accept either JWT or X-Admin-Password header.

    Backward compatibility: existing frontend uses X-Admin-Password,
    new auth flow uses JWT Bearer token. Both work during migration period.
    """
    import os

    # Try JWT first (only if Bearer token provided)
    if credentials:
        try:
            return await get_current_user(credentials)
        except HTTPException as e:
            # Only fall through to admin password for auth failures,
            # not for server errors or other issues
            if e.status_code not in (401, 403):
                raise

    # Fall back to X-Admin-Password (timing-safe comparison)
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    header_pw = request.headers.get("X-Admin-Password", "")
    if admin_pw and header_pw and hmac.compare_digest(admin_pw, header_pw):
        return {"sub": "admin", "email": "admin@local", "role": "admin"}

    raise HTTPException(status_code=401, detail="Authentication required")


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/signup")
async def signup(body: SignupRequest):
    """Register a new user account."""
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    try:
        result = auth_db.create_user(body.email, body.password)
        return {
            "user_id": result["user_id"],
            "email": result["email"],
            "role": result["role"],
            "tier": result["tier"],
            "token": result["token"],
            "expires_in": result["expires_in"],
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"[AUTH] Signup error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Registration failed")


@router.post("/auth/login")
async def login(body: LoginRequest):
    """Authenticate and get JWT token."""
    try:
        result = auth_db.authenticate_user(body.email, body.password)
        return {
            "user_id": result["user_id"],
            "email": result["email"],
            "role": result["role"],
            "tier": result["tier"],
            "token": result["token"],
            "expires_in": result["expires_in"],
        }
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"[AUTH] Login error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Login failed")


@router.get("/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Get current user profile."""
    user_data = auth_db.get_user_by_id(user["sub"])
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "user_id": user_data["id"],
        "email": user_data["email"],
        "role": user_data["role"],
        "tier": user_data["tier"],
        "created_at": user_data["created_at"].isoformat() if user_data.get("created_at") else None,
        "last_login_at": user_data["last_login_at"].isoformat() if user_data.get("last_login_at") else None,
    }
