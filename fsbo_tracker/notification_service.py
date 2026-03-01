"""
FSBO Tracker — Notification service.

Alert matching engine + email dispatch.
Called as a background thread after run_daily() completes.

Matching logic:
1. For each active saved search, query listings updated since last_checked_at
2. Filter by search criteria (market, score, price, type, status, keywords)
3. INSERT into fsbo_notification_matches with ON CONFLICT DO NOTHING (idempotent)
4. Group new matches by user
5. Immediate schedule → dispatch email now
6. Daily schedule → insert fsbo_notifications row with scheduled_for
7. Update search.last_checked_at = now()
"""

import logging
import uuid
from datetime import datetime, timezone

from .db import db_cursor

logger = logging.getLogger("fsbo.notifications")


def match_and_dispatch():
    """Main entry point — run after pipeline completes.

    Safe to call from a daemon thread. All errors caught internally.
    """
    try:
        _run_matching()
    except Exception as e:
        logger.error("[NOTIFY] match_and_dispatch failed: %s", e, exc_info=True)


def _run_matching():
    """Match active saved searches against new/changed listings, then dispatch."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT ss.*, np.email_enabled, np.delivery_schedule, np.timezone,
                   u.email, u.tier
            FROM fsbo_saved_searches ss
            JOIN fsbo_users u ON u.id = ss.user_id
            LEFT JOIN fsbo_notification_prefs np ON np.user_id = ss.user_id
            WHERE ss.is_active = true
              AND u.is_active = true
              AND u.tier != 'free'
        """)
        searches = cur.fetchall()

    if not searches:
        logger.info("[NOTIFY] No active saved searches to match")
        return

    logger.info("[NOTIFY] Matching %d active saved searches", len(searches))

    for search in searches:
        try:
            _match_search(search)
        except Exception as e:
            logger.error("[NOTIFY] Failed matching search %s: %s",
                         search["id"], e, exc_info=True)


def _match_search(search: dict):
    """Match a single saved search against listings."""
    search_id = search["id"]
    user_id = search["user_id"]
    last_checked = search.get("last_checked_at")

    # Build dynamic WHERE clause
    status_filter = search.get("status_filter") or "active"
    conditions = ["l.status = %s"]
    params = [status_filter]

    # Incremental: only new/changed since last check
    if last_checked:
        conditions.append("l.last_seen_at > %s")
        params.append(last_checked)

    # Markets filter
    markets = search.get("markets") or []
    if markets:
        conditions.append("l.search_id = ANY(%s)")
        params.append(markets)

    # Score filter
    min_score = search.get("min_score") or 0
    if min_score > 0:
        conditions.append("COALESCE(l.score, 0) >= %s")
        params.append(min_score)

    # Price range
    if search.get("min_price"):
        conditions.append("l.price >= %s")
        params.append(search["min_price"])
    if search.get("max_price"):
        conditions.append("l.price <= %s")
        params.append(search["max_price"])

    # Listing types (stored in listing_type column, not source)
    listing_types = search.get("listing_types") or ["fsbo", "mlsfsbo"]
    if listing_types:
        conditions.append("l.listing_type = ANY(%s)")
        params.append(listing_types)

    # Min DOM
    if search.get("min_dom"):
        conditions.append("COALESCE(l.dom, 0) >= %s")
        params.append(search["min_dom"])

    # NDVI levels
    ndvi_levels = search.get("ndvi_levels") or []
    if ndvi_levels:
        conditions.append("l.ndvi_overgrowth_level = ANY(%s)")
        params.append(ndvi_levels)

    # Custom keywords (match any keyword in remarks, case-insensitive)
    custom_keywords = search.get("custom_keywords") or []
    if custom_keywords:
        kw_conditions = []
        for kw in custom_keywords:
            kw_conditions.append("LOWER(COALESCE(l.remarks, '')) LIKE %s")
            params.append(f"%{kw.lower()}%")
        if kw_conditions:
            conditions.append(f"({' OR '.join(kw_conditions)})")

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT l.id, l.address, l.city, l.state, l.price, l.score,
               l.search_id, l.source, l.dom, l.beds, l.baths, l.sqft
        FROM fsbo_listings l
        WHERE {where_clause}
    """

    with db_cursor() as (conn, cur):
        cur.execute(query, params)
        matches = cur.fetchall()

        if not matches:
            # Still update cursor even if no matches
            cur.execute("""
                UPDATE fsbo_saved_searches
                SET last_checked_at = now(), updated_at = now()
                WHERE id = %s
            """, (search_id,))
            return

        # Insert into dedup table (idempotent)
        new_match_ids = []
        for m in matches:
            cur.execute("""
                INSERT INTO fsbo_notification_matches (search_id, listing_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING listing_id
            """, (search_id, m["id"]))
            row = cur.fetchone()
            if row:
                new_match_ids.append(m["id"])

        # Update cursor
        cur.execute("""
            UPDATE fsbo_saved_searches
            SET last_checked_at = now(), updated_at = now()
            WHERE id = %s
        """, (search_id,))

    if not new_match_ids:
        return

    # Get full listing data for new matches
    new_listings = [m for m in matches if m["id"] in new_match_ids]
    logger.info("[NOTIFY] Search '%s' matched %d new listings for user %s",
                search.get("name", search_id), len(new_listings), user_id)

    # Dispatch based on user preferences
    email_enabled = search.get("email_enabled")
    if email_enabled is None:
        email_enabled = True  # default

    if not email_enabled:
        return

    schedule = search.get("delivery_schedule") or "daily_9am"

    if schedule == "immediate":
        _send_alert_now(user_id, search, new_listings)
    else:
        _schedule_alert(user_id, search, new_listings, schedule)


def _send_alert_now(user_id: str, search: dict, listings: list):
    """Send alert email immediately."""
    from .email_service import send_alert_email

    email = search.get("email")
    if not email:
        return

    listing_dicts = [dict(l) for l in listings]
    success = send_alert_email(
        to_email=email,
        search_name=search.get("name", "Saved Search"),
        listings=listing_dicts,
    )

    # Log dispatch
    notif_id = str(uuid.uuid4())
    listing_ids = [l["id"] for l in listings]
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_notifications
                (id, user_id, saved_search_id, listing_ids, channel, status, sent_at, content_summary)
            VALUES (%s, %s, %s, %s, 'email', %s, %s, %s)
        """, (
            notif_id, user_id, search["id"],
            __import__("json").dumps(listing_ids),
            "sent" if success else "failed",
            datetime.now(timezone.utc) if success else None,
            f"{len(listings)} listings matched '{search.get('name', '')}'",
        ))


def _schedule_alert(user_id: str, search: dict, listings: list, schedule: str):
    """Schedule alert for later dispatch (daily digest)."""
    scheduled_for = _next_schedule_time(schedule, search.get("timezone", "America/New_York"))

    notif_id = str(uuid.uuid4())
    listing_ids = [l["id"] for l in listings]

    with db_cursor() as (conn, cur):
        # Check if there's already a pending notification for this search
        cur.execute("""
            SELECT id, listing_ids FROM fsbo_notifications
            WHERE saved_search_id = %s AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
        """, (search["id"],))
        existing = cur.fetchone()

        if existing:
            # Merge listing IDs into existing pending notification
            import json
            existing_ids = existing["listing_ids"]
            if isinstance(existing_ids, str):
                existing_ids = json.loads(existing_ids)
            merged = list(set(existing_ids + listing_ids))
            cur.execute("""
                UPDATE fsbo_notifications
                SET listing_ids = %s, content_summary = %s
                WHERE id = %s
            """, (
                json.dumps(merged),
                f"{len(merged)} listings matched '{search.get('name', '')}'",
                existing["id"],
            ))
        else:
            import json
            cur.execute("""
                INSERT INTO fsbo_notifications
                    (id, user_id, saved_search_id, listing_ids, channel, status,
                     scheduled_for, content_summary)
                VALUES (%s, %s, %s, %s, 'email', 'pending', %s, %s)
            """, (
                notif_id, user_id, search["id"],
                json.dumps(listing_ids),
                scheduled_for,
                f"{len(listings)} listings matched '{search.get('name', '')}'",
            ))


def _next_schedule_time(schedule: str, tz_name: str) -> datetime:
    """Calculate next dispatch time based on schedule preference.

    Uses zoneinfo for DST-aware conversion (Python 3.9+).
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    # Map schedule to local hour
    hour_map = {
        "daily_9am": 9,
        "daily_12pm": 12,
        "daily_6pm": 18,
    }
    target_hour = hour_map.get(schedule, 9)

    # Get current time in user's timezone
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, Exception):
        tz = ZoneInfo("America/New_York")

    now_local = datetime.now(tz)

    # Build target time in user's local timezone
    target_local = now_local.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target_local <= now_local:
        target_local += timedelta(days=1)

    # Convert to UTC for storage
    return target_local.astimezone(timezone.utc)


def dispatch_scheduled():
    """Dispatch all pending scheduled notifications whose time has come.

    Called by Railway cron (hourly) or /internal/dispatch-scheduled endpoint.
    Uses pg_advisory_lock to prevent double-dispatch across instances.
    """
    try:
        _dispatch_pending()
    except Exception as e:
        logger.error("[NOTIFY] dispatch_scheduled failed: %s", e, exc_info=True)


def _dispatch_pending():
    """Send all pending notifications that are due."""
    from .email_service import send_alert_email
    import json

    now = datetime.now(timezone.utc)

    with db_cursor() as (conn, cur):
        # Advisory lock — prevents concurrent dispatch from multiple instances
        cur.execute("SELECT pg_try_advisory_lock(42)")
        locked = cur.fetchone()
        if not locked or not locked.get("pg_try_advisory_lock"):
            logger.info("[NOTIFY] Another instance is dispatching, skipping")
            return

        try:
            cur.execute("""
                SELECT n.*, u.email
                FROM fsbo_notifications n
                JOIN fsbo_users u ON u.id = n.user_id
                WHERE n.status = 'pending'
                  AND n.scheduled_for <= %s
                ORDER BY n.created_at
            """, (now,))
            pending = cur.fetchall()

            if not pending:
                return

            logger.info("[NOTIFY] Dispatching %d pending notifications", len(pending))

            for notif in pending:
                try:
                    _dispatch_one(cur, notif)
                except Exception as e:
                    logger.error("[NOTIFY] Failed dispatching %s: %s",
                                 notif["id"], e, exc_info=True)
                    cur.execute("""
                        UPDATE fsbo_notifications SET status = 'failed' WHERE id = %s
                    """, (notif["id"],))
        finally:
            cur.execute("SELECT pg_advisory_unlock(42)")


def _dispatch_one(cur, notif: dict):
    """Dispatch a single pending notification."""
    from .email_service import send_alert_email
    import json

    listing_ids = notif["listing_ids"]
    if isinstance(listing_ids, str):
        listing_ids = json.loads(listing_ids)

    # Fetch listing details for the email
    if listing_ids:
        cur.execute("""
            SELECT id, address, city, state, price, score, beds, baths, sqft
            FROM fsbo_listings
            WHERE id = ANY(%s)
        """, (listing_ids,))
        listings = cur.fetchall()
    else:
        listings = []

    # Get search name
    search_name = "Saved Search"
    if notif.get("saved_search_id"):
        cur.execute("""
            SELECT name FROM fsbo_saved_searches WHERE id = %s
        """, (notif["saved_search_id"],))
        search_row = cur.fetchone()
        if search_row:
            search_name = search_row["name"]

    success = send_alert_email(
        to_email=notif["email"],
        search_name=search_name,
        listings=[dict(l) for l in listings],
    )

    cur.execute("""
        UPDATE fsbo_notifications
        SET status = %s, sent_at = %s
        WHERE id = %s
    """, (
        "sent" if success else "failed",
        datetime.now(timezone.utc) if success else None,
        notif["id"],
    ))
