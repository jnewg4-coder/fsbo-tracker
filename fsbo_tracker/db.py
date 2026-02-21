"""
FSBO Listing Tracker — Database layer (Railway Postgres only, psycopg2 direct)
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, date

import psycopg2
from psycopg2.extras import RealDictCursor


def get_conn():
    """Get a Postgres connection. Requires FSBO_DATABASE_URL — refuses shared DATABASE_URL."""
    url = os.environ.get("FSBO_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "FSBO_DATABASE_URL is not set. "
            "FSBO tracker requires its own database connection to prevent "
            "convolving with the main AVMLens platform DB. "
            "Set FSBO_DATABASE_URL to a valid PostgreSQL connection string."
        )
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


@contextmanager
def db_cursor(commit=True):
    """Context manager yielding (conn, cursor). Auto-commits on success, rolls back on error."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        yield conn, cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _coerce_date(value):
    """Normalize incoming sale-date values for Postgres DATE columns."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------
def run_migration():
    """Execute all migrations in order."""
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    migration_files = sorted(
        f for f in os.listdir(migrations_dir) if f.endswith(".sql")
    )
    with db_cursor() as (conn, cur):
        for mf in migration_files:
            path = os.path.join(migrations_dir, mf)
            with open(path) as f:
                sql = f.read()
            cur.execute(sql)
            print(f"[DB] Migration {mf} applied successfully")


# ---------------------------------------------------------------------------
# Search configs
# ---------------------------------------------------------------------------
def upsert_search(search: dict):
    """Insert or update a search config."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_searches (id, name, region_id, min_lat, max_lat, min_lng, max_lng,
                                       max_price, min_beds, min_dom, grace_days, active)
            VALUES (%(id)s, %(name)s, %(region_id)s, %(min_lat)s, %(max_lat)s, %(min_lng)s, %(max_lng)s,
                    %(max_price)s, %(min_beds)s, %(min_dom)s, %(grace_days)s, TRUE)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                region_id = EXCLUDED.region_id,
                min_lat = EXCLUDED.min_lat, max_lat = EXCLUDED.max_lat,
                min_lng = EXCLUDED.min_lng, max_lng = EXCLUDED.max_lng,
                max_price = EXCLUDED.max_price,
                min_beds = EXCLUDED.min_beds,
                min_dom = EXCLUDED.min_dom
        """, {**search, "min_dom": search.get("min_dom", 55), "grace_days": search.get("grace_days", 3)})


def get_active_searches():
    """Return all active search configs."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT * FROM fsbo_searches WHERE active = TRUE")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Listing upsert + price tracking
# ---------------------------------------------------------------------------
def upsert_listing(listing: dict) -> dict:
    """
    Insert or update a listing. Returns {"action": "new"|"updated"|"price_cut", "old_price": ...}.
    The listing dict must contain at minimum: id, search_id, address, price.
    """
    with db_cursor() as (conn, cur):
        # Lock row to prevent race conditions on concurrent runs
        cur.execute("SELECT id, price, status FROM fsbo_listings WHERE id = %s FOR UPDATE", (listing["id"],))
        existing = cur.fetchone()

        now = datetime.utcnow()
        result = {"action": "new", "old_price": None}

        if existing:
            old_price = existing["price"]
            new_price = listing.get("price")

            # Price drop detection
            if old_price and new_price and new_price < old_price:
                change_pct = round((new_price - old_price) / old_price * 100, 2)
                cur.execute("""
                    INSERT INTO fsbo_price_events (listing_id, price_before, price_after, change_pct)
                    VALUES (%s, %s, %s, %s)
                """, (listing["id"], old_price, new_price, change_pct))
                result = {"action": "price_cut", "old_price": old_price, "change_pct": change_pct}

                cur.execute("""
                    UPDATE fsbo_listings SET
                        price = %s, last_seen_at = %s, days_seen = days_seen + 1,
                        dom = COALESCE(%s, dom),
                        price_cuts = price_cuts + 1,
                        last_price_cut_pct = %s, last_price_cut_at = %s,
                        zestimate = COALESCE(%s, zestimate),
                        rent_zestimate = COALESCE(%s, rent_zestimate),
                        last_sold_price = COALESCE(%s, last_sold_price),
                        last_sold_date = COALESCE(%s, last_sold_date),
                        flood_zone = COALESCE(%s, flood_zone),
                        flood_risk_level = COALESCE(%s, flood_risk_level),
                        status = CASE WHEN status = 'missing' THEN 'active' ELSE status END,
                        grace_until = NULL
                    WHERE id = %s
                """, (
                    new_price, now, listing.get("dom"), change_pct, now,
                    listing.get("zestimate"), listing.get("rent_zestimate"),
                    listing.get("last_sold_price"), _coerce_date(listing.get("last_sold_date")),
                    listing.get("flood_zone"), listing.get("flood_risk_level"),
                    listing["id"]
                ))
            else:
                result = {"action": "updated", "old_price": old_price}
                cur.execute("""
                    UPDATE fsbo_listings SET
                        price = COALESCE(%s, price),
                        last_seen_at = %s,
                        days_seen = days_seen + 1,
                        dom = COALESCE(%s, dom),
                        status = CASE WHEN status = 'missing' THEN 'active' ELSE status END,
                        grace_until = NULL,
                        beds = COALESCE(%s, beds),
                        baths = COALESCE(%s, baths),
                        sqft = COALESCE(%s, sqft),
                        year_built = COALESCE(%s, year_built),
                        zestimate = COALESCE(%s, zestimate),
                        rent_zestimate = COALESCE(%s, rent_zestimate),
                        last_sold_price = COALESCE(%s, last_sold_price),
                        last_sold_date = COALESCE(%s, last_sold_date),
                        flood_zone = COALESCE(%s, flood_zone),
                        flood_risk_level = COALESCE(%s, flood_risk_level)
                    WHERE id = %s
                """, (
                    listing.get("price"), now, listing.get("dom"),
                    listing.get("beds"), listing.get("baths"),
                    listing.get("sqft"), listing.get("year_built"),
                    listing.get("zestimate"), listing.get("rent_zestimate"),
                    listing.get("last_sold_price"), _coerce_date(listing.get("last_sold_date")),
                    listing.get("flood_zone"), listing.get("flood_risk_level"),
                    listing["id"]
                ))
        else:
            # New listing
            cur.execute("""
                INSERT INTO fsbo_listings (
                    id, search_id, source, address, city, state, zip_code,
                    latitude, longitude, listing_type, price,
                    beds, baths, sqft, year_built, property_type,
                    dom, days_seen, status, redfin_url, zillow_url,
                    zestimate, rent_zestimate, last_sold_price, last_sold_date,
                    flood_zone, flood_risk_level,
                    first_seen_at, last_seen_at
                ) VALUES (
                    %(id)s, %(search_id)s, %(source)s, %(address)s, %(city)s, %(state)s, %(zip_code)s,
                    %(latitude)s, %(longitude)s, %(listing_type)s, %(price)s,
                    %(beds)s, %(baths)s, %(sqft)s, %(year_built)s, %(property_type)s,
                    %(dom)s, 1, 'active', %(redfin_url)s, %(zillow_url)s,
                    %(zestimate)s, %(rent_zestimate)s, %(last_sold_price)s, %(last_sold_date)s,
                    %(flood_zone)s, %(flood_risk_level)s,
                    %(now)s, %(now)s
                )
            """, {
                "id": listing["id"],
                "search_id": listing.get("search_id"),
                "source": listing.get("source", "redfin"),
                "address": listing["address"],
                "city": listing.get("city"),
                "state": listing.get("state"),
                "zip_code": listing.get("zip_code"),
                "latitude": listing.get("latitude"),
                "longitude": listing.get("longitude"),
                "listing_type": listing.get("listing_type"),
                "price": listing.get("price"),
                "beds": listing.get("beds"),
                "baths": listing.get("baths"),
                "sqft": listing.get("sqft"),
                "year_built": listing.get("year_built"),
                "property_type": listing.get("property_type"),
                "dom": listing.get("dom"),
                "redfin_url": listing.get("redfin_url"),
                "zillow_url": listing.get("zillow_url"),
                "zestimate": listing.get("zestimate"),
                "rent_zestimate": listing.get("rent_zestimate"),
                "last_sold_price": listing.get("last_sold_price"),
                "last_sold_date": _coerce_date(listing.get("last_sold_date")),
                "flood_zone": listing.get("flood_zone"),
                "flood_risk_level": listing.get("flood_risk_level"),
                "now": now,
            })

        return result


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------
def mark_missing(search_id: str, seen_ids: set, grace_days: int = 3):
    """Mark listings NOT in seen_ids as 'missing' with a grace period.

    Safety: if fewer than 20% of active listings were seen, skip marking —
    the fetch likely failed (proxy blocks) rather than 80%+ delistings.
    """
    now = datetime.utcnow()
    grace_until = now + timedelta(days=grace_days)

    with db_cursor() as (conn, cur):
        if not seen_ids:
            return 0

        # Count current active listings for this market
        cur.execute(
            "SELECT COUNT(*) FROM fsbo_listings WHERE search_id = %s AND status = 'active'",
            (search_id,),
        )
        active_count = cur.fetchone()[0]

        # Safety: if we saw < 20% of active listings, assume fetch failure
        if active_count > 10 and len(seen_ids) < active_count * 0.2:
            print(f"[DB] Skipping mark_missing: only saw {len(seen_ids)}/{active_count} "
                  f"listings for {search_id} — likely proxy failure, not real delistings")
            return 0

        placeholders = ",".join(["%s"] * len(seen_ids))
        cur.execute(f"""
            UPDATE fsbo_listings
            SET status = 'missing', grace_until = %s
            WHERE search_id = %s
              AND status = 'active'
              AND id NOT IN ({placeholders})
        """, [grace_until, search_id] + list(seen_ids))
        return cur.rowcount


def expire_missing():
    """Move listings past grace period from 'missing' to 'gone'."""
    now = datetime.utcnow()
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings
            SET status = 'gone', gone_at = %s
            WHERE status = 'missing' AND grace_until < %s
        """, (now, now))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Detail + score updates
# ---------------------------------------------------------------------------
def update_listing_details(listing_id: str, details: dict):
    """Update remarks, photos, assessed value, seller contact, etc. after detail fetch."""
    now = datetime.utcnow()
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings SET
                remarks = COALESCE(%s, remarks),
                photo_urls = COALESCE(%s, photo_urls),
                assessed_value = COALESCE(%s, assessed_value),
                redfin_estimate = COALESCE(%s, redfin_estimate),
                zestimate = COALESCE(%s, zestimate),
                rent_zestimate = COALESCE(%s, rent_zestimate),
                last_sold_price = COALESCE(%s, last_sold_price),
                last_sold_date = COALESCE(%s, last_sold_date),
                flood_zone = COALESCE(%s, flood_zone),
                flood_risk_level = COALESCE(%s, flood_risk_level),
                seller_name = COALESCE(%s, seller_name),
                seller_phone = COALESCE(%s, seller_phone),
                seller_email = COALESCE(%s, seller_email),
                seller_broker = COALESCE(%s, seller_broker),
                detail_fetched_at = %s
            WHERE id = %s
        """, (
            details.get("remarks"),
            json.dumps(details["photo_urls"]) if details.get("photo_urls") else None,
            details.get("assessed_value"),
            details.get("redfin_estimate"),
            details.get("zestimate"),
            details.get("rent_zestimate"),
            details.get("last_sold_price"),
            _coerce_date(details.get("last_sold_date")),
            details.get("flood_zone"),
            details.get("flood_risk_level"),
            details.get("seller_name"),
            details.get("seller_phone"),
            details.get("seller_email"),
            details.get("seller_broker"),
            now,
            listing_id,
        ))


def update_listing_score(listing_id: str, score: int, breakdown: dict, keywords_matched: list):
    """Update computed score and breakdown."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings SET
                score = %s,
                score_breakdown = %s,
                keywords_matched = %s
            WHERE id = %s
        """, (score, json.dumps(breakdown), json.dumps(keywords_matched), listing_id))


def update_photo_analysis(listing_id: str, analysis: dict):
    """Store photo AI analysis results."""
    now = datetime.utcnow()
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings SET
                photo_damage_score = %s,
                photo_damage_notes = %s,
                photo_analysis_json = %s,
                photo_analyzed_at = %s
            WHERE id = %s
        """, (
            analysis.get("damage_score"),
            analysis.get("damage_notes"),
            json.dumps(analysis),
            now,
            listing_id,
        ))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def get_listings_needing_details(limit: int = 50):
    """Listings with no detail fetch yet (active only)."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT id, redfin_url, zillow_url, source
            FROM fsbo_listings
            WHERE status IN ('active', 'watched')
              AND detail_fetched_at IS NULL
            ORDER BY first_seen_at ASC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def get_listings_missing_remarks(limit: int = 50):
    """Active listings missing real descriptions (null, empty, or short snippets < 100 chars)."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT id, address, city, state, zip_code, redfin_url, zillow_url, source
            FROM fsbo_listings
            WHERE status IN ('active', 'watched')
              AND (remarks IS NULL OR remarks = '' OR LENGTH(remarks) < 100)
            ORDER BY score DESC, first_seen_at ASC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def bootstrap_price_cut(listing_id: str, price_change: int, current_price: int,
                        change_date=None):
    """
    Persist a Zillow-detected price cut for a listing that has price_cuts=0.
    Sets price_cuts=1, inserts a fsbo_price_events record, and updates cut metadata.
    Only bootstraps if price_cuts is currently 0 (doesn't double-count).

    change_date can be a Unix timestamp in ms (from Zillow), a datetime, or None.
    """
    if not price_change or price_change >= 0:
        return False

    cut_amount = abs(price_change)
    price_before = current_price + cut_amount
    if price_before <= 0:
        return False
    change_pct = round((current_price - price_before) / price_before * 100, 2)

    # Convert Zillow epoch-ms to Python datetime
    cut_at = None
    if change_date:
        if isinstance(change_date, (int, float)) and change_date > 1_000_000_000_000:
            # Epoch milliseconds
            cut_at = datetime.utcfromtimestamp(change_date / 1000)
        elif isinstance(change_date, datetime):
            cut_at = change_date

    with db_cursor() as (conn, cur):
        # Lock row + only bootstrap if price_cuts is still 0
        cur.execute(
            "SELECT price_cuts FROM fsbo_listings WHERE id = %s FOR UPDATE",
            (listing_id,),
        )
        row = cur.fetchone()
        if not row or (row["price_cuts"] or 0) > 0:
            return False

        now = datetime.utcnow()
        event_time = cut_at or now

        # Insert price event record
        cur.execute("""
            INSERT INTO fsbo_price_events (listing_id, price_before, price_after, change_pct, detected_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (listing_id, price_before, current_price, change_pct, event_time))

        # Update listing price_cuts metadata
        cur.execute("""
            UPDATE fsbo_listings SET
                price_cuts = 1,
                last_price_cut_pct = %s,
                last_price_cut_at = %s
            WHERE id = %s
        """, (change_pct, event_time, listing_id))

    return True


def update_listing_remarks(listing_id: str, remarks: str = None, redfin_url: str = None,
                           redfin_estimate: int = None, assessed_value: int = None,
                           zestimate: int = None, rent_zestimate: int = None,
                           last_sold_price: int = None, last_sold_date=None,
                           flood_zone: str = None, flood_risk_level: str = None,
                           seller_name: str = None, seller_phone: str = None,
                           seller_email: str = None, seller_broker: str = None):
    """Update remarks, seller contact, and optional valuation/sale/flood fields."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings SET
                remarks = COALESCE(%s, remarks),
                redfin_url = COALESCE(%s, redfin_url),
                redfin_estimate = COALESCE(%s, redfin_estimate),
                assessed_value = COALESCE(%s, assessed_value),
                zestimate = COALESCE(%s, zestimate),
                rent_zestimate = COALESCE(%s, rent_zestimate),
                last_sold_price = COALESCE(%s, last_sold_price),
                last_sold_date = COALESCE(%s, last_sold_date),
                flood_zone = COALESCE(%s, flood_zone),
                flood_risk_level = COALESCE(%s, flood_risk_level),
                seller_name = COALESCE(%s, seller_name),
                seller_phone = COALESCE(%s, seller_phone),
                seller_email = COALESCE(%s, seller_email),
                seller_broker = COALESCE(%s, seller_broker),
                detail_fetched_at = COALESCE(detail_fetched_at, %s)
            WHERE id = %s
        """, (
            remarks, redfin_url, redfin_estimate, assessed_value,
            zestimate, rent_zestimate, last_sold_price, _coerce_date(last_sold_date),
            flood_zone, flood_risk_level,
            seller_name, seller_phone, seller_email, seller_broker,
            datetime.utcnow(), listing_id,
        ))


def update_listing_flood(listing_id: str, flood_zone: str = None, flood_risk_level: str = None):
    """Persist flood zone summary for a listing."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings
            SET flood_zone = COALESCE(%s, flood_zone),
                flood_risk_level = COALESCE(%s, flood_risk_level)
            WHERE id = %s
        """, (flood_zone, flood_risk_level, listing_id))


def get_listings_for_photo_ai(keyword_threshold: int = 10, price_ratio_threshold: float = 0.90):
    """Listings that qualify for photo AI but haven't been analyzed yet."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT l.id, l.photo_urls, l.price, l.assessed_value, l.redfin_estimate,
                   l.score, l.dom, l.days_seen, l.price_cuts,
                   COALESCE((l.score_breakdown::json->>'keywords')::int, 0) as keyword_score
            FROM fsbo_listings l
            WHERE l.status IN ('active', 'watched')
              AND l.photo_urls IS NOT NULL
              AND l.photo_analyzed_at IS NULL
              AND l.score_breakdown IS NOT NULL
              AND (
                  COALESCE((l.score_breakdown::json->>'keywords')::int, 0) >= %s
                  OR (l.price > 0 AND l.assessed_value > 0
                      AND l.price::float / l.assessed_value <= %s)
                  OR (COALESCE(l.dom, l.days_seen) >= 55 AND l.price_cuts >= 1)
              )
            ORDER BY l.score DESC
            LIMIT 20
        """, (keyword_threshold, price_ratio_threshold))
        return cur.fetchall()


def get_active_listings(search_id: str = None, min_score: int = 0):
    """Get active/watched listings, optionally filtered by search and min score."""
    with db_cursor(commit=False) as (conn, cur):
        query = """
            SELECT * FROM fsbo_listings
            WHERE status IN ('active', 'watched')
              AND score >= %s
        """
        params = [min_score]
        if search_id:
            query += " AND search_id = %s"
            params.append(search_id)
        query += " ORDER BY score DESC, last_seen_at DESC"
        cur.execute(query, params)
        return cur.fetchall()


def get_price_history(listing_id: str):
    """Get price events for a listing."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT * FROM fsbo_price_events
            WHERE listing_id = %s
            ORDER BY detected_at DESC
        """, (listing_id,))
        return cur.fetchall()


def get_tracker_stats():
    """Quick stats for the dashboard header."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'active') as active_count,
                COUNT(*) FILTER (WHERE status = 'watched') as watched_count,
                COUNT(*) FILTER (WHERE score >= 60 AND status IN ('active', 'watched')) as high_priority,
                COUNT(*) FILTER (WHERE first_seen_at > NOW() - INTERVAL '3 days'
                                  AND status IN ('active', 'watched')) as new_recent,
                COUNT(*) FILTER (WHERE last_price_cut_at > NOW() - INTERVAL '7 days'
                                  AND status IN ('active', 'watched')) as recent_cuts
            FROM fsbo_listings
        """)
        return cur.fetchone()


def set_watched(listing_id: str):
    """Toggle a listing to 'watched' status."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_listings SET status = 'watched' WHERE id = %s
        """, (listing_id,))
