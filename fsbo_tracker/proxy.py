"""
FSBO Proxy Manager — Sticky sessions with smart rotation.

IPRoyal sticky sessions: same session ID = same IP for ~10 minutes.
Only rotate when blocked. OxyLabs Web Unblocker as fallback.
"""

import os
import time
import random
import string
from typing import Optional

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
_current_session_id: Optional[str] = None
_session_created_at: float = 0
_consecutive_failures: int = 0

# Sticky session duration: 10 minutes (IPRoyal default max)
STICKY_DURATION = 600
# After this many consecutive failures, switch to OxyLabs
OXYLABS_THRESHOLD = 3


def _generate_session_id() -> str:
    """Generate a random session ID."""
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"fsbo_{suffix}"


def _get_session_id(force_new: bool = False) -> str:
    """Get current sticky session ID, or create a new one if expired/forced."""
    global _current_session_id, _session_created_at

    now = time.time()
    age = now - _session_created_at

    if force_new or _current_session_id is None or age > STICKY_DURATION:
        old = _current_session_id
        _current_session_id = _generate_session_id()
        _session_created_at = now
        if old:
            print(f"[Proxy] Rotated session: {old} → {_current_session_id} (age={age:.0f}s)")
        else:
            print(f"[Proxy] New session: {_current_session_id}")

    return _current_session_id


def get_iproyal_proxy(force_new_session: bool = False) -> Optional[dict]:
    """
    Get IPRoyal residential proxy with sticky session.
    Same session ID = same exit IP for up to 10 minutes.
    """
    host = os.getenv("IPROYAL_HOST")
    port = os.getenv("IPROYAL_PORT")
    user = os.getenv("IPROYAL_USER")
    password = os.getenv("IPROYAL_PASS")

    if not all([host, port, user, password]):
        return None

    session_id = _get_session_id(force_new=force_new_session)
    password_with_session = f"{password}_session-{session_id}"
    proxy_url = f"http://{user}:{password_with_session}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def get_oxylabs_proxy() -> Optional[dict]:
    """Get OxyLabs Web Unblocker proxy (fallback)."""
    username = os.getenv("OXYLABS_USERNAME")
    password = os.getenv("OXYLABS_PASSWORD")
    host = os.getenv("OXYLABS_HOST", "unblock.oxylabs.io")
    port = os.getenv("OXYLABS_PORT", "60000")

    if not username or not password:
        return None

    proxy_url = f"http://{username}:{password}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def get_proxy(force_new_session: bool = False) -> Optional[dict]:
    """
    Get best available proxy.
    - Uses IPRoyal with sticky session by default.
    - Falls back to OxyLabs after consecutive failures.
    - Pass force_new_session=True to rotate IP after a block.
    """
    global _consecutive_failures

    if _consecutive_failures >= OXYLABS_THRESHOLD:
        oxy = get_oxylabs_proxy()
        if oxy:
            print(f"[Proxy] Using OxyLabs fallback (after {_consecutive_failures} IPRoyal failures)")
            return oxy

    return get_iproyal_proxy(force_new_session=force_new_session)


def record_success():
    """Call after a successful request — keeps current sticky session.
    Resets failure counter so next get_proxy() returns to IPRoyal."""
    global _consecutive_failures
    was_on_oxylabs = _consecutive_failures >= OXYLABS_THRESHOLD
    _consecutive_failures = 0
    if was_on_oxylabs:
        print("[Proxy] Success on OxyLabs — returning to IPRoyal")


def record_failure():
    """Record a non-burn failure (timeout, parse error). Does NOT rotate IP.
    Use burn_session() for definitive blocks (403, captcha)."""
    global _consecutive_failures
    _consecutive_failures += 1
    print(f"[Proxy] Failure #{_consecutive_failures} (keeping same IP)")


def burn_session(reason: str = "block"):
    """
    Burn current session — force new IP on next get_proxy() call.
    Call on definitive blocks (403, captcha, 429).
    """
    global _consecutive_failures, _current_session_id, _session_created_at
    _consecutive_failures += 1
    _current_session_id = None
    _session_created_at = 0
    print(f"[Proxy] Burned session ({reason}) — failure #{_consecutive_failures}, rotating IP")


def get_status() -> dict:
    """Get current proxy status for debugging."""
    return {
        "session_id": _current_session_id,
        "session_age_s": round(time.time() - _session_created_at, 1) if _session_created_at else 0,
        "consecutive_failures": _consecutive_failures,
        "using_oxylabs": _consecutive_failures >= OXYLABS_THRESHOLD,
    }
