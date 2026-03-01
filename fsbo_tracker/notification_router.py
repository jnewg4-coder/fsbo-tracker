"""
FSBO Tracker — Notification endpoints.

Saved search CRUD, notification preferences, notification history.
Server-side tier limits enforced (Starter: 1, Growth: 3, Pro: unlimited).
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from .auth_router import get_current_user
from .access import get_entitlements, TIER_CONFIGS
from . import auth_db
from .db import db_cursor
from .rate_limit import limiter

logger = logging.getLogger("fsbo.notification_router")

router = APIRouter(prefix="/fsbo/notifications", tags=["notifications"])

# Internal dispatch secret — set DISPATCH_SECRET env var on Railway
DISPATCH_SECRET = os.environ.get("DISPATCH_SECRET", "")


def _get_full_entitlements(jwt_user: dict) -> dict:
    """Resolve entitlements from the full DB user record (not bare JWT).

    JWT payload lacks subscription_status/period_end, so get_entitlements()
    would wrongly downgrade paid tiers to free. Fetch full record first.
    """
    user_id = jwt_user.get("sub") or jwt_user.get("id")
    db_user = auth_db.get_user_by_id(user_id)
    if not db_user:
        return get_entitlements(None)
    return get_entitlements(db_user)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class SavedSearchCreate(BaseModel):
    name: str
    markets: list[str] = []
    min_score: int = 0
    max_price: Optional[int] = None
    min_price: Optional[int] = None
    listing_types: list[str] = ["fsbo", "mlsfsbo"]
    status_filter: str = "active"
    min_dom: Optional[int] = None
    ndvi_levels: Optional[list[str]] = None
    custom_keywords: Optional[list[str]] = None


class SavedSearchUpdate(BaseModel):
    name: Optional[str] = None
    markets: Optional[list[str]] = None
    min_score: Optional[int] = None
    max_price: Optional[int] = None
    min_price: Optional[int] = None
    listing_types: Optional[list[str]] = None
    status_filter: Optional[str] = None
    min_dom: Optional[int] = None
    ndvi_levels: Optional[list[str]] = None
    custom_keywords: Optional[list[str]] = None
    is_active: Optional[bool] = None


class NotificationPrefsUpdate(BaseModel):
    email_enabled: Optional[bool] = None
    delivery_schedule: Optional[str] = None
    timezone: Optional[str] = None


# ---------------------------------------------------------------------------
# Saved search CRUD
# ---------------------------------------------------------------------------
@router.get("/saved-searches")
async def list_saved_searches(user=Depends(get_current_user)):
    """List all saved searches for the current user."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT ss.*,
                   (SELECT COUNT(*) FROM fsbo_notification_matches nm
                    WHERE nm.search_id = ss.id) as total_matches
            FROM fsbo_saved_searches ss
            WHERE ss.user_id = %s
            ORDER BY ss.created_at DESC
        """, (user_id,))
        searches = cur.fetchall()

    return {"success": True, "searches": [dict(s) for s in searches]}


@router.post("/saved-searches")
@limiter.limit("10/minute")
async def create_saved_search(
    request: Request,
    body: SavedSearchCreate,
    user=Depends(get_current_user),
):
    """Create a new saved search. Enforces tier limits."""
    user_id = user.get("sub") or user.get("id")
    ents = _get_full_entitlements(user)
    tier = ents.get("tier", "free")

    max_allowed = ents.get("max_saved_searches", 0)
    if max_allowed == 0:
        raise HTTPException(
            status_code=403,
            detail="Upgrade to Starter to create saved searches",
        )

    # Count existing
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT COUNT(*) as cnt FROM fsbo_saved_searches WHERE user_id = %s
        """, (user_id,))
        existing = cur.fetchone()["cnt"]

    if existing >= max_allowed:
        tier_labels = {"starter": "Starter", "growth": "Growth", "pro": "Pro"}
        raise HTTPException(
            status_code=403,
            detail=f"Your {tier_labels.get(tier, tier)} plan allows {max_allowed} saved search(es). Upgrade for more.",
        )

    search_id = str(uuid.uuid4())
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_saved_searches
                (id, user_id, name, markets, min_score, max_price, min_price,
                 listing_types, status_filter, min_dom, ndvi_levels, custom_keywords,
                 created_via)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'form')
            RETURNING *
        """, (
            search_id, user_id, body.name,
            json.dumps(body.markets), body.min_score,
            body.max_price, body.min_price,
            json.dumps(body.listing_types), body.status_filter,
            body.min_dom,
            json.dumps(body.ndvi_levels) if body.ndvi_levels else None,
            json.dumps(body.custom_keywords) if body.custom_keywords else None,
        ))
        created = cur.fetchone()

    logger.info("[NOTIFY] User %s created saved search '%s' (%s)", user_id, body.name, search_id)
    return {"success": True, "search": dict(created)}


@router.put("/saved-searches/{search_id}")
async def update_saved_search(
    search_id: str,
    body: SavedSearchUpdate,
    user=Depends(get_current_user),
):
    """Update an existing saved search."""
    user_id = user.get("sub") or user.get("id")

    # Build SET clause from non-None fields
    updates = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.markets is not None:
        updates["markets"] = json.dumps(body.markets)
    if body.min_score is not None:
        updates["min_score"] = body.min_score
    if body.max_price is not None:
        updates["max_price"] = body.max_price
    if body.min_price is not None:
        updates["min_price"] = body.min_price
    if body.listing_types is not None:
        updates["listing_types"] = json.dumps(body.listing_types)
    if body.status_filter is not None:
        updates["status_filter"] = body.status_filter
    if body.min_dom is not None:
        updates["min_dom"] = body.min_dom
    if body.ndvi_levels is not None:
        updates["ndvi_levels"] = json.dumps(body.ndvi_levels)
    if body.custom_keywords is not None:
        updates["custom_keywords"] = json.dumps(body.custom_keywords)
    if body.is_active is not None:
        updates["is_active"] = body.is_active

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["updated_at"] = datetime.now(timezone.utc)

    set_parts = [f"{k} = %s" for k in updates]
    set_clause = ", ".join(set_parts)
    values = list(updates.values())

    with db_cursor() as (conn, cur):
        cur.execute(f"""
            UPDATE fsbo_saved_searches
            SET {set_clause}
            WHERE id = %s AND user_id = %s
            RETURNING *
        """, values + [search_id, user_id])
        updated = cur.fetchone()

    if not updated:
        raise HTTPException(status_code=404, detail="Saved search not found")

    return {"success": True, "search": dict(updated)}


@router.delete("/saved-searches/{search_id}")
async def delete_saved_search(
    search_id: str,
    user=Depends(get_current_user),
):
    """Delete a saved search and its matches (CASCADE)."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor() as (conn, cur):
        cur.execute("""
            DELETE FROM fsbo_saved_searches
            WHERE id = %s AND user_id = %s
            RETURNING id
        """, (search_id, user_id))
        deleted = cur.fetchone()

    if not deleted:
        raise HTTPException(status_code=404, detail="Saved search not found")

    return {"success": True}


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------
@router.get("/preferences")
async def get_notification_prefs(user=Depends(get_current_user)):
    """Get notification preferences for the current user."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT * FROM fsbo_notification_prefs WHERE user_id = %s
        """, (user_id,))
        prefs = cur.fetchone()

    if not prefs:
        # Return defaults
        return {
            "success": True,
            "preferences": {
                "email_enabled": True,
                "delivery_schedule": "daily_9am",
                "timezone": "America/New_York",
            },
        }

    return {"success": True, "preferences": dict(prefs)}


@router.put("/preferences")
async def update_notification_prefs(
    body: NotificationPrefsUpdate,
    user=Depends(get_current_user),
):
    """Update notification preferences (upsert)."""
    user_id = user.get("sub") or user.get("id")

    valid_schedules = {"immediate", "daily_9am", "daily_12pm", "daily_6pm"}
    if body.delivery_schedule and body.delivery_schedule not in valid_schedules:
        raise HTTPException(status_code=400, detail=f"Invalid schedule. Choose from: {', '.join(valid_schedules)}")

    valid_timezones = {
        "America/New_York", "America/Chicago", "America/Denver",
        "America/Los_Angeles", "America/Phoenix",
    }
    if body.timezone and body.timezone not in valid_timezones:
        raise HTTPException(status_code=400, detail=f"Unsupported timezone")

    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_notification_prefs (user_id, email_enabled, delivery_schedule, timezone)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                email_enabled = COALESCE(%s, fsbo_notification_prefs.email_enabled),
                delivery_schedule = COALESCE(%s, fsbo_notification_prefs.delivery_schedule),
                timezone = COALESCE(%s, fsbo_notification_prefs.timezone),
                updated_at = now()
            RETURNING *
        """, (
            user_id,
            body.email_enabled if body.email_enabled is not None else True,
            body.delivery_schedule or "daily_9am",
            body.timezone or "America/New_York",
            body.email_enabled,
            body.delivery_schedule,
            body.timezone,
        ))
        prefs = cur.fetchone()

    return {"success": True, "preferences": dict(prefs)}


# ---------------------------------------------------------------------------
# Notification history
# ---------------------------------------------------------------------------
@router.get("/history")
async def get_notification_history(user=Depends(get_current_user)):
    """Get recent notification history for the current user."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT n.*, ss.name as search_name
            FROM fsbo_notifications n
            LEFT JOIN fsbo_saved_searches ss ON ss.id = n.saved_search_id
            WHERE n.user_id = %s
            ORDER BY n.created_at DESC
            LIMIT 50
        """, (user_id,))
        notifications = cur.fetchall()

    return {"success": True, "notifications": [dict(n) for n in notifications]}


# ---------------------------------------------------------------------------
# Internal: scheduled dispatch (Railway cron)
# ---------------------------------------------------------------------------
@router.post("/internal/dispatch-scheduled", include_in_schema=False)
async def dispatch_scheduled_endpoint(request: Request):
    """Called by Railway cron at :00 of each hour.

    Only sends if current hour matches user prefs.
    Protected by pg_advisory_lock — safe to call concurrently.
    Requires DISPATCH_SECRET header for auth (or localhost).
    """
    # Authenticate: require secret header or localhost origin
    secret = request.headers.get("X-Dispatch-Secret", "")
    client_host = request.client.host if request.client else ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost")
    if not is_local and (not DISPATCH_SECRET or secret != DISPATCH_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    from .notification_service import dispatch_scheduled
    dispatch_scheduled()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entitlements helper: saved search limits for frontend
# ---------------------------------------------------------------------------
@router.get("/limits")
async def get_notification_limits(user=Depends(get_current_user)):
    """Return saved search limits and current usage."""
    user_id = user.get("sub") or user.get("id")
    ents = _get_full_entitlements(user)
    tier = ents.get("tier", "free")

    max_allowed = ents.get("max_saved_searches", 0)

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT COUNT(*) as cnt FROM fsbo_saved_searches WHERE user_id = %s
        """, (user_id,))
        current = cur.fetchone()["cnt"]

    return {
        "success": True,
        "tier": tier,
        "max_saved_searches": max_allowed,
        "current_saved_searches": current,
        "can_create": current < max_allowed,
    }
