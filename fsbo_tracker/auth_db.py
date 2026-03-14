"""FSBO Tracker — User/auth database operations.

Separate from listing db.py to keep auth concerns isolated.
Uses the same FSBO_DATABASE_URL connection.
"""

import uuid
from datetime import datetime, timedelta

import psycopg2

from .db import db_cursor
from .auth_service import (
    hash_password, verify_password, create_access_token,
    generate_verification_code, generate_reset_token,
    MAX_LOGIN_ATTEMPTS, LOCKOUT_MINUTES,
)


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
def create_user(email: str, password: str, role: str = "user") -> dict:
    """Create a new user with verification code. Returns user dict with JWT."""
    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)
    now = datetime.utcnow()
    code = generate_verification_code()
    code_expires = now + timedelta(hours=24)

    with db_cursor() as (conn, cur):
        try:
            cur.execute("""
                INSERT INTO fsbo_users
                    (id, email, password_hash, role, tier, created_at,
                     email_verified, verification_code, verification_expires_at)
                VALUES (%s, %s, %s, %s, 'free', %s, FALSE, %s, %s)
            """, (user_id, email.lower(), pw_hash, role, now, code, code_expires))
        except psycopg2.IntegrityError:
            conn.rollback()
            raise ValueError("Email already registered")

    token, expires_in = create_access_token(
        user_id, email.lower(), role, tier="free", token_version=0,
    )
    return {
        "user_id": user_id,
        "email": email.lower(),
        "role": role,
        "tier": "free",
        "token": token,
        "expires_in": expires_in,
        "verification_code": code,
    }


def authenticate_user(email: str, password: str) -> dict:
    """Authenticate user by email/password. Returns user dict with JWT.

    Enforces brute-force protection: 5 attempts → 15min lockout.
    """
    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT id, email, password_hash, role, tier, is_active,
                   failed_login_attempts, locked_until, token_version
            FROM fsbo_users WHERE email = %s
        """, (email.lower(),))
        user = cur.fetchone()

        if not user:
            raise ValueError("Invalid email or password")

        if not user["is_active"]:
            raise ValueError("Account is deactivated")

        # Check lockout
        now = datetime.utcnow()
        if user["locked_until"] and user["locked_until"] > now:
            remaining = int((user["locked_until"] - now).total_seconds() / 60) + 1
            raise ValueError(f"Account locked. Try again in {remaining} minutes")

        # Google-only accounts have no password — same generic error
        if not user["password_hash"]:
            raise ValueError("Invalid email or password")

        # Verify password
        if not verify_password(password, user["password_hash"]):
            attempts = (user["failed_login_attempts"] or 0) + 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                cur.execute("""
                    UPDATE fsbo_users SET failed_login_attempts = %s, locked_until = %s
                    WHERE id = %s
                """, (attempts, locked_until, user["id"]))
                conn.commit()  # persist before raise (raise triggers rollback in db_cursor)
                raise ValueError(f"Too many attempts. Locked for {LOCKOUT_MINUTES} minutes")
            else:
                cur.execute("""
                    UPDATE fsbo_users SET failed_login_attempts = %s WHERE id = %s
                """, (attempts, user["id"]))
                conn.commit()  # persist before raise
                raise ValueError("Invalid email or password")

        # Success — reset failure counter
        cur.execute("""
            UPDATE fsbo_users SET failed_login_attempts = 0, locked_until = NULL,
            last_login_at = %s WHERE id = %s
        """, (now, user["id"]))

    token, expires_in = create_access_token(
        user["id"], user["email"], user["role"],
        tier=user["tier"],
        token_version=user.get("token_version") or 0,
    )
    return {
        "user_id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "tier": user["tier"],
        "token": token,
        "expires_in": expires_in,
    }


def get_user_by_id(user_id: str) -> dict:
    """Get user by ID (for JWT validation and profile display)."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT id, email, role, tier, is_active, created_at, last_login_at,
                   token_version, selected_market, market_selected_at,
                   market_grace_used, allowed_markets,
                   subscription_status, subscription_period_end,
                   ai_actions_today, ai_actions_reset_date,
                   email_verified,
                   advisor_enabled, advisor_messages_used, advisor_messages_limit,
                   advisor_reset_date, advisor_subscription_id
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        user = cur.fetchone()
        if not user:
            return None
        return dict(user)


def get_token_version(user_id: str) -> int:
    """Get current token_version for JWT staleness check."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute(
            "SELECT token_version FROM fsbo_users WHERE id = %s AND is_active = TRUE",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return -1  # user not found or inactive
        return row["token_version"] or 0


def user_exists(user_id: str) -> bool:
    """Quick existence check for JWT validation."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT 1 FROM fsbo_users WHERE id = %s AND is_active = TRUE", (user_id,))
        return cur.fetchone() is not None


def update_user_tier(user_id: str, tier: str):
    """Update user subscription tier and bump token_version.

    Bumping token_version invalidates all existing JWTs, forcing re-login
    so the user gets fresh claims with the new tier.
    """
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users
            SET tier = %s, token_version = COALESCE(token_version, 0) + 1
            WHERE id = %s
        """, (tier, user_id))


def select_market(user_id: str, market_id: str) -> dict:
    """Select or switch market for free-tier user.

    Atomic enforcement of 30-day lock + 1 grace switch within first 7 days.

    Returns: {"selected_market": str, "grace_used": bool} on success.
    Raises ValueError if switch denied (locked).
    """
    with db_cursor() as (conn, cur):
        # First attempt: user has never selected a market
        cur.execute("""
            UPDATE fsbo_users
            SET selected_market = %s, market_selected_at = NOW()
            WHERE id = %s AND selected_market IS NULL
            RETURNING selected_market
        """, (market_id, user_id))
        if cur.fetchone():
            return {"selected_market": market_id, "grace_used": False}

        # Second attempt: grace switch (within first 7 days, grace not yet used)
        cur.execute("""
            UPDATE fsbo_users
            SET selected_market = %s, market_selected_at = NOW(), market_grace_used = TRUE
            WHERE id = %s
              AND market_grace_used = FALSE
              AND market_selected_at > NOW() - INTERVAL '7 days'
              AND selected_market != %s
            RETURNING selected_market
        """, (market_id, user_id, market_id))
        if cur.fetchone():
            return {"selected_market": market_id, "grace_used": True}

        # Third attempt: 30-day cooldown has expired
        cur.execute("""
            UPDATE fsbo_users
            SET selected_market = %s, market_selected_at = NOW(), market_grace_used = FALSE
            WHERE id = %s
              AND market_selected_at < NOW() - INTERVAL '30 days'
            RETURNING selected_market
        """, (market_id, user_id))
        if cur.fetchone():
            return {"selected_market": market_id, "grace_used": False}

        # All conditions failed — user is locked
        # Get current state for error message
        cur.execute("""
            SELECT selected_market, market_selected_at, market_grace_used
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        state = cur.fetchone()

        if state and state["market_selected_at"]:
            days_left = 30 - (datetime.utcnow() - state["market_selected_at"]).days
            days_left = max(1, days_left)
            raise ValueError(
                f"Market locked to {state['selected_market']}. "
                f"You can switch in {days_left} days."
            )
        raise ValueError("Unable to switch market")


def select_markets(user_id: str, market_ids: list) -> dict:
    """Select or switch markets for growth-tier user (up to 3).

    Same 30-day lock + 7-day grace as single-market selection.
    Uses market_selected_at + market_grace_used (shared with select_market).

    Returns: {"allowed_markets": list, "grace_used": bool}
    Raises ValueError if switch denied (locked).
    """
    import json

    market_json = json.dumps(sorted(market_ids))

    with db_cursor() as (conn, cur):
        # First attempt: user has never selected markets (allowed_markets empty/null)
        cur.execute("""
            UPDATE fsbo_users
            SET allowed_markets = %s::jsonb, market_selected_at = NOW()
            WHERE id = %s AND (allowed_markets IS NULL OR allowed_markets = '[]'::jsonb)
            RETURNING allowed_markets
        """, (market_json, user_id))
        if cur.fetchone():
            return {"allowed_markets": sorted(market_ids), "grace_used": False}

        # Second attempt: grace switch (within 7 days, grace not yet used)
        cur.execute("""
            UPDATE fsbo_users
            SET allowed_markets = %s::jsonb, market_selected_at = NOW(), market_grace_used = TRUE
            WHERE id = %s
              AND market_grace_used = FALSE
              AND market_selected_at > NOW() - INTERVAL '7 days'
              AND allowed_markets != %s::jsonb
            RETURNING allowed_markets
        """, (market_json, user_id, market_json))
        if cur.fetchone():
            return {"allowed_markets": sorted(market_ids), "grace_used": True}

        # Third attempt: 30-day cooldown expired
        cur.execute("""
            UPDATE fsbo_users
            SET allowed_markets = %s::jsonb, market_selected_at = NOW(), market_grace_used = FALSE
            WHERE id = %s
              AND market_selected_at < NOW() - INTERVAL '30 days'
            RETURNING allowed_markets
        """, (market_json, user_id))
        if cur.fetchone():
            return {"allowed_markets": sorted(market_ids), "grace_used": False}

        # Locked — build error message
        cur.execute("""
            SELECT allowed_markets, market_selected_at, market_grace_used
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        state = cur.fetchone()

        if state and state["market_selected_at"]:
            days_left = 30 - (datetime.utcnow() - state["market_selected_at"]).days
            days_left = max(1, days_left)
            raise ValueError(
                f"Markets locked. You can switch in {days_left} days."
            )
        raise ValueError("Unable to switch markets")


# ---------------------------------------------------------------------------
# Email verification + password reset
# ---------------------------------------------------------------------------
def verify_email_code(user_id: str, code: str) -> bool:
    """Verify 6-digit email code. Returns True on success, raises ValueError on failure."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users
            SET email_verified = TRUE, verification_code = NULL, verification_expires_at = NULL
            WHERE id = %s
              AND verification_code = %s
              AND verification_expires_at > NOW()
              AND email_verified = FALSE
            RETURNING id
        """, (user_id, code))
        if cur.fetchone():
            return True

        # Check why it failed
        cur.execute("""
            SELECT email_verified, verification_code, verification_expires_at
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        user = cur.fetchone()
        if not user:
            raise ValueError("User not found")
        if user["email_verified"]:
            raise ValueError("Email already verified")
        if user["verification_expires_at"] and user["verification_expires_at"] < datetime.utcnow():
            raise ValueError("Code expired. Request a new one")
        raise ValueError("Invalid verification code")


def resend_verification_code(user_id: str) -> str:
    """Generate new verification code. Returns the code."""
    code = generate_verification_code()
    expires = datetime.utcnow() + timedelta(hours=24)
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users
            SET verification_code = %s, verification_expires_at = %s
            WHERE id = %s AND email_verified = FALSE
            RETURNING id
        """, (code, expires, user_id))
        if not cur.fetchone():
            raise ValueError("Already verified or user not found")
    return code


def create_password_reset_token(email: str) -> dict:
    """Create password reset token. Returns {user_id, token} or raises ValueError."""
    token = generate_reset_token()
    expires = datetime.utcnow() + timedelta(hours=1)
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users
            SET password_reset_token = %s, password_reset_expires_at = %s
            WHERE email = %s AND is_active = TRUE
            RETURNING id, email
        """, (token, expires, email.lower()))
        row = cur.fetchone()
        if not row:
            raise ValueError("no_user")  # Don't leak whether email exists
        return {"user_id": row["id"], "email": row["email"], "token": token}


def reset_password(token: str, new_password: str) -> bool:
    """Consume reset token and set new password. Returns True on success."""
    pw_hash = hash_password(new_password)
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users
            SET password_hash = %s,
                password_reset_token = NULL,
                password_reset_expires_at = NULL,
                token_version = COALESCE(token_version, 0) + 1
            WHERE password_reset_token = %s
              AND password_reset_expires_at > NOW()
            RETURNING id
        """, (pw_hash, token))
        if cur.fetchone():
            return True
        raise ValueError("Invalid or expired reset link")


def find_or_create_google_user(
    email: str, google_id: str, picture: str = None,
) -> dict:
    """Find existing user by google_id or email, or create a new OAuth user.

    Returns user dict with JWT token.
    """
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, email, role, tier, google_id, token_version "
            "FROM fsbo_users WHERE google_id = %s OR email = %s LIMIT 1",
            (google_id, email.lower()),
        )
        user = cur.fetchone()

        if user:
            if not user.get("google_id"):
                cur.execute(
                    "UPDATE fsbo_users SET google_id = %s, google_picture = %s, email_verified = TRUE WHERE id = %s",
                    (google_id, picture, user["id"]),
                )
            else:
                cur.execute(
                    "UPDATE fsbo_users SET google_picture = %s, email_verified = TRUE WHERE id = %s",
                    (picture, user["id"]),
                )
            cur.execute(
                "UPDATE fsbo_users SET last_login_at = %s WHERE id = %s",
                (datetime.utcnow(), user["id"]),
            )
            user_id = user["id"]
            user_email = user["email"]
            user_role = user["role"]
            user_tier = user["tier"]
            tv = user.get("token_version") or 0
            is_new = False
        else:
            user_id = str(uuid.uuid4())
            user_email = email.lower()
            user_role = "user"
            user_tier = "free"
            tv = 0
            is_new = True
            cur.execute("""
                INSERT INTO fsbo_users
                    (id, email, password_hash, role, tier, google_id, google_picture, email_verified, created_at)
                VALUES (%s, %s, NULL, %s, %s, %s, %s, TRUE, %s)
            """, (user_id, user_email, user_role, user_tier, google_id, picture, datetime.utcnow()))

    token, expires_in = create_access_token(
        user_id, user_email, user_role, tier=user_tier, token_version=tv,
    )
    return {
        "user_id": user_id,
        "email": user_email,
        "role": user_role,
        "tier": user_tier,
        "token": token,
        "expires_in": expires_in,
        "is_new": is_new,
    }


def get_user_count() -> int:
    """Total registered users."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM fsbo_users")
        return cur.fetchone()[0]
