"""FSBO Tracker — Google OAuth service.

Ported from AVMLens oauth_service.py, adapted for FSBO standalone deployment.
Uses HMAC-signed stateless state tokens (survives redeploys, no memory leaks).
"""

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx

# Google OAuth configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://fsbo-api-production.up.railway.app/api/v2/auth/google/callback",
)

# Google OAuth endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

GOOGLE_SCOPES = ["openid", "email", "profile"]

STATE_TTL_SECONDS = 300  # 5 minutes

# Signing key: JWT_SECRET (always available), fall back to random per-process
_STATE_SECRET = os.getenv("JWT_SECRET", "") or secrets.token_hex(32)


def is_google_oauth_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _sign_state(payload: str) -> str:
    """HMAC-SHA256 sign a payload string."""
    return hmac.new(
        _STATE_SECRET.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()[:32]


def generate_signed_state(redirect_after: Optional[str] = None) -> str:
    """Create a stateless HMAC-signed state token.

    Format: nonce.timestamp[.redirect]|signature
    """
    nonce = secrets.token_urlsafe(16)
    ts = str(int(time.time()))
    parts = f"{nonce}.{ts}"
    if redirect_after:
        parts = f"{parts}.{redirect_after}"
    sig = _sign_state(parts)
    return f"{parts}|{sig}"


def verify_signed_state(state: str) -> Tuple[bool, Optional[str]]:
    """Verify HMAC signature and TTL. Returns (valid, redirect_after)."""
    if "|" not in state:
        return False, None

    parts, sig = state.rsplit("|", 1)
    expected_sig = _sign_state(parts)

    if not hmac.compare_digest(sig, expected_sig):
        return False, None

    segments = parts.split(".", 2)
    if len(segments) < 2:
        return False, None

    try:
        ts = int(segments[1])
    except ValueError:
        return False, None

    if time.time() - ts > STATE_TTL_SECONDS:
        return False, None

    redirect_after = segments[2] if len(segments) == 3 else None
    return True, redirect_after


def get_google_auth_url(state: str) -> str:
    if not is_google_oauth_configured():
        raise ValueError("Google OAuth not configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    if not is_google_oauth_configured():
        raise ValueError("Google OAuth not configured")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": GOOGLE_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )
        if response.status_code != 200:
            error_data = response.json() if response.text else {}
            raise ValueError(
                f"Failed to exchange code: {error_data.get('error_description', 'Unknown error')}"
            )
        return response.json()


async def get_google_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        if response.status_code != 200:
            raise ValueError("Failed to get user info from Google")
        return response.json()
