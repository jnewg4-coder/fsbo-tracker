"""FSBO Tracker — Entitlement engine + global serializer.

Single source of truth for tier permissions, field redaction, AI limits,
and access audit logging. Every API response goes through serialize_response().

Design principles:
- Never send locked fields to the frontend (backend-enforced)
- One missed endpoint cannot leak full data (global serializer, not route-by-route)
- Atomic DB operations for limits (UPDATE...RETURNING, no read-then-write)
- Audit log for every denial and redaction
"""

import hashlib
import logging
import re
import threading
from datetime import date, datetime
from typing import Dict, Optional, Set

from .config import TOTAL_MARKETS
from .db import db_cursor

logger = logging.getLogger("fsbo.access")

# ---------------------------------------------------------------------------
# Salt for coordinate jitter — prevents reverse-engineering exact location
# from jittered values. Change this to re-randomize all jitter offsets.
# ---------------------------------------------------------------------------
_JITTER_SALT = "fsbo-coord-jitter-v1-9f3a"

# ---------------------------------------------------------------------------
# Tier configuration — single dict defines all per-tier capabilities
# ---------------------------------------------------------------------------
TIER_CONFIGS: Dict[str, dict] = {
    "guest": {
        "max_markets": 0,
        "redact": True,
        "export_csv": False,
        "ai_actions_per_day": 0,
        "deals": False,
        "show_ndvi_detail": False,
        "max_saved_searches": 0,
    },
    "free": {
        "max_markets": 1,
        "redact": True,
        "export_csv": False,
        "ai_actions_per_day": 0,
        "deals": False,
        "show_ndvi_detail": False,
        "max_saved_searches": 0,
    },
    "starter": {
        "max_markets": 1,
        "redact": False,
        "export_csv": False,
        "ai_actions_per_day": 5,
        "deals": True,
        "show_ndvi_detail": True,
        "max_saved_searches": 1,
    },
    "growth": {
        "max_markets": 3,
        "redact": False,
        "export_csv": True,
        "ai_actions_per_day": 20,
        "deals": True,
        "show_ndvi_detail": True,
        "max_saved_searches": 3,
    },
    "pro": {
        "max_markets": TOTAL_MARKETS,
        "redact": False,
        "export_csv": True,
        "ai_actions_per_day": 100,
        "deals": True,
        "show_ndvi_detail": True,
        "max_saved_searches": 999,
    },
}

# ---------------------------------------------------------------------------
# Fields to redact for free/guest tiers
# ---------------------------------------------------------------------------
REDACTED_FIELDS: Set[str] = {
    # PII / seller contact
    "seller_name", "seller_phone", "seller_email", "seller_broker",
    # Source URLs (can identify exact property)
    "redfin_url", "zillow_url",
    # Photos (visual identification)
    "photo_urls", "photos",
    # Remarks (often contain address, cross-streets, identifying details)
    "remarks",
    # Sale history (cross-referenceable with public records)
    "last_sold_price", "last_sold_date", "sold_price", "sold_date",
    # AI analysis detail
    "photo_analysis_json", "photo_damage_notes",
    # NDVI detail (keep badge-level ndvi_overgrowth_level only)
    "ndvi_mean", "ndvi_overgrowth_pct", "ndvi_confidence",
}

# Timestamp fields to truncate to date-only for free/guest
TIMESTAMP_FIELDS: Set[str] = {
    "first_seen_at", "last_seen_at", "created_at", "updated_at",
    "status_changed_at", "detail_fetched_at", "photo_analyzed_at",
    "ndvi_checked_at", "gone_at", "grace_until",
}

# Fields that are always shown regardless of tier
# (publicly available data that makes the product useful enough to convert)
ALWAYS_VISIBLE: Set[str] = {
    "id", "address", "city", "state", "zip", "price",
    "beds", "baths", "sqft", "year_built", "lot_size",
    "assessed_value", "redfin_estimate", "zestimate", "rent_zestimate",
    "score", "score_breakdown", "keywords_matched",
    "status", "listing_status", "dom", "days_seen", "price_cuts",
    "photo_damage_score",  # integer score, part of breakdown
    "ndvi_overgrowth_level",  # badge only (HIGH/MODERATE/LOW/MINIMAL)
    "search_id", "source",
    "latitude", "longitude",  # will be jittered separately
    "last_price_cut_at",  # just a date, not PII
}


# ---------------------------------------------------------------------------
# Entitlements — derived from user record + tier config
# ---------------------------------------------------------------------------
def get_entitlements(user: Optional[dict]) -> dict:
    """Build entitlements dict from user record (or guest if None).

    Args:
        user: User dict from DB or JWT payload. None = guest.
              Expected keys: sub/id, email, role, tier,
              selected_market, allowed_markets, ai_actions_today,
              ai_actions_reset_date.

    Returns:
        Flat dict with all permission flags + user context.
    """
    if user is None:
        return {
            "tier": "guest",
            "role": "guest",
            "user_id": None,
            **TIER_CONFIGS["guest"],
            "allowed_markets": [],
            "selected_market": None,
            "is_admin": False,
        }

    role = user.get("role", "user")
    tier = user.get("tier", "free")

    # Enforce subscription expiry: if paid tier but subscription lapsed, downgrade
    if tier not in ("free", "guest") and role != "admin":
        sub_status = user.get("subscription_status")
        period_end = user.get("subscription_period_end")
        if sub_status in ("cancelled", "past_due", None):
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if period_end is None:
                tier = "free"
            elif isinstance(period_end, str):
                tier = "free" if datetime.fromisoformat(period_end.replace("Z", "+00:00")) < now else tier
            elif isinstance(period_end, datetime):
                # Coerce naive DB timestamps to UTC before comparing
                pe = period_end if period_end.tzinfo else period_end.replace(tzinfo=timezone.utc)
                tier = "free" if pe < now else tier

    # Admin bypasses all restrictions
    if role == "admin":
        return {
            "tier": tier,
            "role": "admin",
            "user_id": user.get("sub") or user.get("id"),
            "max_markets": 999,
            "redact": False,
            "export_csv": True,
            "ai_actions_per_day": 9999,
            "deals": True,
            "show_ndvi_detail": True,
            "max_saved_searches": 999,
            "allowed_markets": [],  # empty = all
            "selected_market": None,
            "is_admin": True,
        }

    config = TIER_CONFIGS.get(tier, TIER_CONFIGS["free"])

    # Build allowed markets list
    allowed = user.get("allowed_markets") or []
    selected = user.get("selected_market")
    if isinstance(allowed, str):
        # Shouldn't happen with JSONB, but defensive
        import json
        try:
            allowed = json.loads(allowed)
        except (ValueError, TypeError):
            allowed = []

    # Free tier: single selected market
    if tier == "free" and selected:
        allowed = [selected]
    # Pro: all markets (empty list = no filter)
    elif tier == "pro":
        allowed = []

    return {
        "tier": tier,
        "role": role,
        "user_id": user.get("sub") or user.get("id"),
        **config,
        "allowed_markets": allowed,
        "selected_market": selected,
        "is_admin": False,
    }


# ---------------------------------------------------------------------------
# Address redaction: 11323 Astoria Dr → 11300 block Astoria Dr
# ---------------------------------------------------------------------------
_ADDR_RE = re.compile(r"^(\d+)\s+(.+)$")


def _redact_address(address: str) -> str:
    """Round house number to nearest 100 and append 'block'."""
    if not address:
        return address
    m = _ADDR_RE.match(address.strip())
    if not m:
        return address  # non-standard format, leave as-is
    num = int(m.group(1))
    street = m.group(2)
    block = (num // 100) * 100
    return f"{block} block {street}"


# ---------------------------------------------------------------------------
# Coordinate jitter: consistent per-listing, 0.002–0.005° offset
# (~200-550m depending on latitude)
# ---------------------------------------------------------------------------
def _jitter_coord(value: float, listing_id: str, axis: str) -> float:
    """Apply deterministic jitter to a coordinate.

    Seeded by hash(listing_id + salt + axis) so:
    - Same listing always gets same jitter (consistent)
    - Different listings get different jitter (not a fixed offset)
    - Cannot reverse-engineer without the salt
    """
    if value is None or not listing_id:
        return value
    seed = hashlib.sha256(f"{listing_id}{_JITTER_SALT}{axis}".encode()).digest()
    # Use first 4 bytes as unsigned int, normalize to [0, 1)
    raw = int.from_bytes(seed[:4], "big") / (2**32)
    # Map to range [0.002, 0.005]
    magnitude = 0.002 + raw * 0.003
    # Use 5th byte to determine sign
    sign = 1 if seed[4] % 2 == 0 else -1
    return round(value + (sign * magnitude), 6)


# ---------------------------------------------------------------------------
# Timestamp truncation: datetime → date-only string
# ---------------------------------------------------------------------------
def _truncate_timestamp(value) -> Optional[str]:
    """Convert datetime to date-only ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        # Already ISO string — truncate to date portion
        return value[:10] if len(value) >= 10 else value
    return None


# ---------------------------------------------------------------------------
# Core redaction function
# ---------------------------------------------------------------------------
def redact_listing(listing: dict, entitlements: dict) -> dict:
    """Apply tier-based redaction to a single listing dict.

    Returns a NEW dict (never mutates the original).
    Paid tiers and admin get the listing unchanged.
    """
    if not entitlements.get("redact", True):
        return dict(listing)

    out = {}
    listing_id = listing.get("id", "")

    for key, value in listing.items():
        # Redacted fields → null
        if key in REDACTED_FIELDS:
            out[key] = None
            continue

        # Address → block-level
        if key == "address":
            out[key] = _redact_address(value) if value else value
            continue

        # Coordinates → jitter
        if key == "latitude" and value is not None:
            out[key] = _jitter_coord(float(value), listing_id, "lat")
            continue
        if key == "longitude" and value is not None:
            out[key] = _jitter_coord(float(value), listing_id, "lng")
            continue

        # Timestamps → date-only
        if key in TIMESTAMP_FIELDS:
            out[key] = _truncate_timestamp(value)
            continue

        # Everything else passes through
        out[key] = value

    out["_redacted"] = True
    return out


# ---------------------------------------------------------------------------
# Market filtering
# ---------------------------------------------------------------------------
def filter_by_market(listings: list, entitlements: dict) -> list:
    """Filter listings to only allowed markets.

    Admin and pro (empty allowed_markets) see everything.
    Free/starter with no market selected see NOTHING (empty list).
    Returns filtered list (never mutates original).
    """
    # Admin and pro bypass market filtering
    if entitlements.get("is_admin") or entitlements.get("tier") == "pro":
        return listings

    allowed = entitlements.get("allowed_markets", [])

    # No markets selected → return empty (fail closed, not open)
    if not allowed:
        return []

    return [
        l for l in listings
        if l.get("search_id") in allowed
    ]


def filter_searches(searches: list, entitlements: dict) -> list:
    """Filter market searches to only allowed markets."""
    if entitlements.get("is_admin") or entitlements.get("tier") == "pro":
        return searches

    allowed = entitlements.get("allowed_markets", [])
    if not allowed:
        return []

    return [s for s in searches if s.get("id") in allowed]


# ---------------------------------------------------------------------------
# Global serializer — every response goes through this
# ---------------------------------------------------------------------------
def serialize_response(data: dict, entitlements: dict) -> dict:
    """Apply redaction + market filtering to any API response.

    Handles common response shapes:
    - {"listings": [...], ...} → redact each listing, filter by market
    - {"searches": [...]} → filter to allowed markets
    - Single listing dict (has "id" + "address") → redact
    - Other dicts → pass through unchanged

    Adds _redacted flag if redaction was applied.
    """
    if not isinstance(data, dict):
        return data

    result = dict(data)

    # Response contains a listings array
    if "listings" in result and isinstance(result["listings"], list):
        filtered = filter_by_market(result["listings"], entitlements)
        if entitlements.get("redact", True):
            result["listings"] = [redact_listing(l, entitlements) for l in filtered]
            result["_redacted"] = True
        else:
            result["listings"] = filtered

    # Response contains searches array
    if "searches" in result and isinstance(result["searches"], list):
        result["searches"] = filter_searches(result["searches"], entitlements)

    # Single listing response (has id + address, not wrapped in "listings")
    if "id" in result and "address" in result and "listings" not in result:
        if entitlements.get("redact", True):
            result = redact_listing(result, entitlements)

    return result


# ---------------------------------------------------------------------------
# AI action daily limits (atomic — no race condition)
# ---------------------------------------------------------------------------
def check_ai_limit(user_id: str, tier: str) -> bool:
    """Check and atomically increment AI action counter.

    Uses UPDATE...RETURNING to avoid read-then-write race.
    Resets counter if date has rolled over.

    Returns True if action is allowed, False if limit exceeded.
    """
    config = TIER_CONFIGS.get(tier, TIER_CONFIGS["free"])
    max_actions = config["ai_actions_per_day"]

    if max_actions <= 0:
        return False

    today = date.today()

    with db_cursor() as (conn, cur):
        # First: reset counter if date has rolled over
        cur.execute("""
            UPDATE fsbo_users
            SET ai_actions_today = 0, ai_actions_reset_date = %s
            WHERE id = %s AND (ai_actions_reset_date IS NULL OR ai_actions_reset_date < %s)
        """, (today, user_id, today))

        # Atomic increment — only succeeds if under limit
        cur.execute("""
            UPDATE fsbo_users
            SET ai_actions_today = ai_actions_today + 1
            WHERE id = %s AND ai_actions_today < %s
            RETURNING ai_actions_today
        """, (user_id, max_actions))

        row = cur.fetchone()
        return row is not None


def get_ai_usage(user_id: str, tier: str) -> dict:
    """Get current AI usage stats for display."""
    config = TIER_CONFIGS.get(tier, TIER_CONFIGS["free"])
    max_actions = config["ai_actions_per_day"]

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT ai_actions_today, ai_actions_reset_date
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        row = cur.fetchone()

    if not row:
        return {"used": 0, "limit": max_actions, "remaining": max_actions}

    today = date.today()
    used = row["ai_actions_today"] or 0
    reset_date = row["ai_actions_reset_date"]

    # If date rolled over, counter is effectively 0
    if reset_date is None or reset_date < today:
        used = 0

    return {
        "used": used,
        "limit": max_actions,
        "remaining": max(0, max_actions - used),
    }


# ---------------------------------------------------------------------------
# Market access check
# ---------------------------------------------------------------------------
def check_market_access(search_id: str, entitlements: dict) -> bool:
    """Check if user has access to a specific market.

    Returns True if allowed, False if denied.
    Admin and pro always have access. Free with no market = denied.
    """
    if entitlements.get("is_admin") or entitlements.get("tier") == "pro":
        return True
    allowed = entitlements.get("allowed_markets", [])
    if not allowed:
        return False  # fail closed — no market selected
    return search_id in allowed


# ---------------------------------------------------------------------------
# Feature gates
# ---------------------------------------------------------------------------
def can_use_deals(entitlements: dict) -> bool:
    """Check if user tier includes deal pipeline access."""
    return entitlements.get("deals", False) or entitlements.get("is_admin", False)


def can_export_csv(entitlements: dict) -> bool:
    """Check if user tier includes CSV export."""
    return entitlements.get("export_csv", False) or entitlements.get("is_admin", False)


def can_use_saved_searches(entitlements: dict) -> bool:
    """Check if user tier includes saved search alerts."""
    return entitlements.get("max_saved_searches", 0) > 0 or entitlements.get("is_admin", False)


# ---------------------------------------------------------------------------
# Access audit log (fire-and-forget, non-blocking)
# ---------------------------------------------------------------------------
def log_access(
    user_id: Optional[str],
    action: str,
    resource_id: Optional[str],
    tier: Optional[str],
    result: str,
    detail: Optional[str] = None,
):
    """Log an access event to fsbo_access_log.

    Fire-and-forget in a background thread to avoid slowing responses.

    Actions: listing_view, listing_redacted, ai_allowed, ai_denied,
             market_denied, export_denied, deal_denied, deal_allowed
    Results: allowed, denied, redacted
    """
    def _insert():
        try:
            with db_cursor() as (conn, cur):
                cur.execute("""
                    INSERT INTO fsbo_access_log
                        (user_id, action, resource_id, tier, result, detail)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, action, resource_id, tier, result, detail))
        except Exception as e:
            logger.warning(f"[ACCESS] Failed to log access event: {e}")

    threading.Thread(target=_insert, daemon=True).start()
