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
    MAX_LOGIN_ATTEMPTS, LOCKOUT_MINUTES,
)


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
def create_user(email: str, password: str, role: str = "user") -> dict:
    """Create a new user. Returns user dict with JWT."""
    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)
    now = datetime.utcnow()

    with db_cursor() as (conn, cur):
        try:
            cur.execute("""
                INSERT INTO fsbo_users (id, email, password_hash, role, tier, created_at)
                VALUES (%s, %s, %s, %s, 'free', %s)
            """, (user_id, email.lower(), pw_hash, role, now))
        except psycopg2.IntegrityError:
            conn.rollback()
            raise ValueError("Email already registered")

    token, expires_in = create_access_token(user_id, email.lower(), role)
    return {
        "user_id": user_id,
        "email": email.lower(),
        "role": role,
        "tier": "free",
        "token": token,
        "expires_in": expires_in,
    }


def authenticate_user(email: str, password: str) -> dict:
    """Authenticate user by email/password. Returns user dict with JWT.

    Enforces brute-force protection: 5 attempts → 15min lockout.
    """
    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT id, email, password_hash, role, tier, is_active,
                   failed_login_attempts, locked_until
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

        # Verify password
        if not verify_password(password, user["password_hash"]):
            attempts = (user["failed_login_attempts"] or 0) + 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                cur.execute("""
                    UPDATE fsbo_users SET failed_login_attempts = %s, locked_until = %s
                    WHERE id = %s
                """, (attempts, locked_until, user["id"]))
                raise ValueError(f"Too many attempts. Locked for {LOCKOUT_MINUTES} minutes")
            else:
                cur.execute("""
                    UPDATE fsbo_users SET failed_login_attempts = %s WHERE id = %s
                """, (attempts, user["id"]))
                raise ValueError("Invalid email or password")

        # Success — reset failure counter
        cur.execute("""
            UPDATE fsbo_users SET failed_login_attempts = 0, locked_until = NULL,
            last_login_at = %s WHERE id = %s
        """, (now, user["id"]))

    token, expires_in = create_access_token(user["id"], user["email"], user["role"])
    return {
        "user_id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "tier": user["tier"],
        "token": token,
        "expires_in": expires_in,
    }


def get_user_by_id(user_id: str) -> dict:
    """Get user by ID (for JWT validation)."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT id, email, role, tier, is_active, created_at, last_login_at
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        user = cur.fetchone()
        if not user:
            return None
        return dict(user)


def user_exists(user_id: str) -> bool:
    """Quick existence check for JWT validation."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT 1 FROM fsbo_users WHERE id = %s AND is_active = TRUE", (user_id,))
        return cur.fetchone() is not None


def update_user_tier(user_id: str, tier: str):
    """Update user subscription tier."""
    with db_cursor() as (conn, cur):
        cur.execute("UPDATE fsbo_users SET tier = %s WHERE id = %s", (tier, user_id))


def get_user_count() -> int:
    """Total registered users."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM fsbo_users")
        return cur.fetchone()[0]
