"""FSBO Tracker — Auth API endpoints (signup, login, me, market selection).

Phase 1: email/password auth + JWT + entitlements.
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
from .access import get_entitlements, get_ai_usage, TIER_CONFIGS
from .rate_limit import limiter

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

class SelectMarketRequest(BaseModel):
    market_id: str


# ---------------------------------------------------------------------------
# JWT dependencies
# ---------------------------------------------------------------------------

# In-memory cache: user_id -> (timestamp, token_version)
# Short TTL to limit window after deactivation or tier change
_user_cache: dict = {}
_CACHE_TTL = 60


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Validate JWT, check token_version staleness, return payload.

    Token version check: if the user's DB token_version is higher than
    what's in the JWT (claim "tv"), the JWT is stale (tier changed since
    token was issued). Return 401 to force re-login with fresh claims.
    """
    if credentials:
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jwt_tv = payload.get("tv", 0)

        now = time.time()
        cache_key = user_id

        if cache_key in _user_cache:
            cached_at, cached_tv = _user_cache[cache_key]
            if now - cached_at < _CACHE_TTL:
                if cached_tv < 0:
                    # Cached as inactive
                    raise HTTPException(status_code=401, detail="Account no longer active")
                if jwt_tv < cached_tv:
                    raise HTTPException(
                        status_code=401,
                        detail="Session expired — please log in again",
                    )
                return payload

        # Cache miss or expired — check DB
        db_tv = auth_db.get_token_version(user_id)
        if db_tv < 0:
            # User not found or inactive
            _user_cache[cache_key] = (now, -1)
            raise HTTPException(status_code=401, detail="Account no longer active")

        _user_cache[cache_key] = (now, db_tv)

        if jwt_tv < db_tv:
            raise HTTPException(
                status_code=401,
                detail="Session expired — please log in again",
            )

        return payload

    raise HTTPException(status_code=401, detail="Authentication required")


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """Return user payload if valid JWT provided, None otherwise.

    For endpoints that work for both authenticated and guest users (e.g. /demo).
    """
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


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
        return {"sub": "admin", "email": "admin@local", "role": "admin", "tier": "pro", "tv": 0}

    raise HTTPException(status_code=401, detail="Authentication required")


async def get_user_with_entitlements(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Auth dependency that returns user payload enriched with entitlements.

    Used by protected endpoints that need to know tier permissions.
    Returns dict with standard JWT claims + full entitlements block.
    """
    user = await get_current_user_or_admin(request, credentials)

    # For admin via X-Admin-Password, entitlements are admin-level
    if user.get("role") == "admin":
        return {**user, "entitlements": get_entitlements(user)}

    # For JWT users, fetch full user record for entitlement fields
    user_data = auth_db.get_user_by_id(user["sub"])
    if not user_data:
        raise HTTPException(status_code=401, detail="Account no longer active")

    entitlements = get_entitlements(user_data)
    return {**user, "entitlements": entitlements}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/signup")
@limiter.limit("5/minute")
async def signup(request: Request, body: SignupRequest):
    """Register a new user account."""
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    try:
        result = auth_db.create_user(body.email, body.password)

        # Slack notification for new signup
        try:
            from .slack_alerts import get_alerter
            get_alerter().notify_signup(body.email)
        except Exception:
            pass

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
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest):
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
    """Get current user profile with entitlements."""
    user_data = auth_db.get_user_by_id(user["sub"])
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    entitlements = get_entitlements(user_data)
    ai_usage = get_ai_usage(user["sub"], user_data["tier"])

    return {
        "user_id": user_data["id"],
        "email": user_data["email"],
        "role": user_data["role"],
        "tier": user_data["tier"],
        "created_at": user_data["created_at"].isoformat() if user_data.get("created_at") else None,
        "last_login_at": user_data["last_login_at"].isoformat() if user_data.get("last_login_at") else None,
        "entitlements": entitlements,
        "ai_usage": ai_usage,
        "subscription_status": user_data.get("subscription_status", "none"),
        "subscription_period_end": (
            user_data["subscription_period_end"].isoformat()
            if user_data.get("subscription_period_end") else None
        ),
    }


@router.post("/auth/select-market")
async def select_market(body: SelectMarketRequest, user: dict = Depends(get_current_user)):
    """Select or switch market (free tier: 1 market, 30-day lock, 1 grace switch)."""
    from .config import SEARCHES

    # Validate market_id exists
    valid_ids = {s["id"] for s in SEARCHES}
    if body.market_id not in valid_ids:
        raise HTTPException(status_code=400, detail=f"Invalid market: {body.market_id}")

    try:
        result = auth_db.select_market(user["sub"], body.market_id)
        return {
            "selected_market": result["selected_market"],
            "grace_used": result["grace_used"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[AUTH] Select market error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to select market")
