"""
FSBO Listing Tracker — Daily orchestrator
Wires fetchers → dedup → upsert → state transitions → detail fetch → scoring → photo AI → JSON export.
"""

import json
import os
import random
import time
from datetime import datetime

from .config import (
    SEARCHES, REDFIN_DELAY, ZILLOW_DELAY, SHORTLIST_MIN_SCORE, DEFAULT_GRACE_DAYS,
    INTER_MARKET_DELAY_MIN, INTER_MARKET_DELAY_MAX,
)
from . import db
from . import redfin_fetcher
from . import zillow_fetcher
from . import scorer
from . import photo_analyzer
from .geo_lite import lookup_flood_zone


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def _deduplicate(redfin_listings: list, zillow_listings: list) -> list:
    """
    Merge Redfin + Zillow listings, deduplicating by address.
    Redfin is primary (more structured DOM). Zillow supplements with
    assessed value and Zillow-specific valuation fields.
    """
    by_address = {}

    # Redfin first (primary)
    for l in redfin_listings:
        key = _normalize_address(l.get("address", ""))
        if key:
            by_address[key] = l

    # Zillow second — only add if not already seen
    added_from_zillow = 0
    for l in zillow_listings:
        key = _normalize_address(l.get("address", ""))
        if not key:
            continue
        if key in by_address:
            # Merge supplemental data from Zillow into Redfin listing
            existing = by_address[key]
            if not existing.get("assessed_value") and l.get("assessed_value"):
                existing["assessed_value"] = l["assessed_value"]
            if not existing.get("zestimate") and l.get("zestimate"):
                existing["zestimate"] = l["zestimate"]
            if not existing.get("rent_zestimate") and l.get("rent_zestimate"):
                existing["rent_zestimate"] = l["rent_zestimate"]
            if not existing.get("last_sold_price") and l.get("last_sold_price"):
                existing["last_sold_price"] = l["last_sold_price"]
            if not existing.get("last_sold_date") and l.get("last_sold_date"):
                existing["last_sold_date"] = l["last_sold_date"]
            if not existing.get("zillow_url") and l.get("zillow_url"):
                existing["zillow_url"] = l["zillow_url"]
        else:
            by_address[key] = l
            added_from_zillow += 1

    total = len(by_address)
    print(f"[Tracker] Deduped: {len(redfin_listings)} Redfin + {len(zillow_listings)} Zillow → {total} unique ({added_from_zillow} Zillow-only)")
    return list(by_address.values())


def _normalize_address(addr: str) -> str:
    """Normalize address for dedup comparison."""
    if not addr:
        return ""
    return addr.lower().strip().replace(".", "").replace(",", "").replace("  ", " ")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run_daily(
    markets: list = None,
    skip_redfin: bool = False,
    skip_zillow: bool = False,
    skip_details: bool = False,
    skip_descriptions: bool = False,
    skip_photos: bool = False,
    dry_run: bool = False,
):
    """
    Run the full daily pipeline.

    Args:
        markets: List of market IDs to process (None = all active).
        skip_redfin: Skip Redfin fetch.
        skip_zillow: Skip Zillow fetch.
        skip_details: Skip detail fetch pass.
        skip_descriptions: Skip Redfin description cross-reference.
        skip_photos: Skip photo AI analysis.
        dry_run: Fetch and parse but don't write to DB.

    Returns:
        Summary dict with counts.
    """
    start = time.time()
    summary = {
        "markets": [],
        "total_fetched": 0,
        "new": 0,
        "updated": 0,
        "price_cuts": 0,
        "status_changes": 0,
        "details_fetched": 0,
        "descriptions_found": 0,
        "photos_analyzed": 0,
        "scored": 0,
        "high_priority": 0,
        "flood_checked": 0,
        "flood_updated": 0,
        "ndvi_checked": 0,
        "ndvi_updated": 0,
        "errors": [],
    }

    # Sync config → DB so new markets are always picked up
    try:
        for s in SEARCHES:
            db.upsert_search(s)
    except Exception as e:
        print(f"[Tracker] Warning: search sync failed: {e}")

    # Load search configs
    searches = _get_searches(markets)
    if not searches:
        print("[Tracker] No active searches found")
        return summary

    # -----------------------------------------------------------------------
    # Step 1: Fetch listings from each market
    # -----------------------------------------------------------------------
    for market_idx, search in enumerate(searches):
        market_id = search["id"]
        market_name = search.get("name", market_id)

        # Inter-market jitter: pause between markets to avoid burst traffic
        if market_idx > 0:
            jitter = random.uniform(INTER_MARKET_DELAY_MIN, INTER_MARKET_DELAY_MAX)
            print(f"\n[Tracker] Pausing {jitter:.0f}s before next market...")
            time.sleep(jitter)

        print(f"\n{'='*60}")
        print(f"[Tracker] Market {market_idx + 1}/{len(searches)}: {market_name}")
        print(f"{'='*60}")

        redfin_listings = []
        zillow_listings = []

        # Redfin fetch
        if not skip_redfin:
            try:
                redfin_listings = redfin_fetcher.fetch_listings(search)
                time.sleep(REDFIN_DELAY)
            except Exception as e:
                msg = f"Redfin fetch failed for {market_id}: {e}"
                print(f"[Tracker] ERROR: {msg}")
                summary["errors"].append(msg)

        # Zillow fetch
        if not skip_zillow:
            try:
                zillow_listings = zillow_fetcher.fetch_listings(search)
                time.sleep(ZILLOW_DELAY)
            except Exception as e:
                msg = f"Zillow fetch failed for {market_id}: {e}"
                print(f"[Tracker] ERROR: {msg}")
                summary["errors"].append(msg)

        # Deduplicate
        all_listings = _deduplicate(redfin_listings, zillow_listings)
        summary["total_fetched"] += len(all_listings)
        summary["markets"].append({
            "id": market_id,
            "name": market_name,
            "redfin_count": len(redfin_listings),
            "zillow_count": len(zillow_listings),
            "deduped_count": len(all_listings),
        })

        if dry_run:
            print(f"[Tracker] DRY RUN — {len(all_listings)} listings parsed, not writing to DB")
            for l in all_listings[:5]:
                p = f"${l['price']:,}" if l.get('price') is not None else "$?"
                print(f"  {l['address']} | {p} | DOM:{l.get('dom', '?')} | {l['source']}")
            if len(all_listings) > 5:
                print(f"  ... and {len(all_listings) - 5} more")
            continue

        # -------------------------------------------------------------------
        # Step 2: Upsert listings (price drop detection happens here)
        # -------------------------------------------------------------------
        seen_ids = set()
        for listing in all_listings:
            try:
                result = db.upsert_listing(listing)
                seen_ids.add(listing["id"])

                if result["action"] == "new":
                    summary["new"] += 1
                elif result["action"] == "price_cut":
                    summary["price_cuts"] += 1
                    old_p = f"${result['old_price']:,}" if result.get('old_price') is not None else "$?"
                    new_p = f"${listing['price']:,}" if listing.get('price') is not None else "$?"
                    print(f"  PRICE CUT: {listing['address']} {old_p} → {new_p} ({result.get('change_pct', 0):.1f}%)")
                else:
                    summary["updated"] += 1

                # Log status transitions
                if result.get("status_change"):
                    summary["status_changes"] += 1
                    print(f"  STATUS: {listing['address']} {result['status_change']}")

                # Update supplemental data (assessed value, photos, etc.)
                extras = {}
                if listing.get("assessed_value"):
                    extras["assessed_value"] = listing["assessed_value"]
                if listing.get("redfin_estimate"):
                    extras["redfin_estimate"] = listing["redfin_estimate"]
                if listing.get("zestimate"):
                    extras["zestimate"] = listing["zestimate"]
                if listing.get("rent_zestimate"):
                    extras["rent_zestimate"] = listing["rent_zestimate"]
                if listing.get("last_sold_price"):
                    extras["last_sold_price"] = listing["last_sold_price"]
                if listing.get("last_sold_date"):
                    extras["last_sold_date"] = listing["last_sold_date"]
                if listing.get("remarks"):
                    extras["remarks"] = listing["remarks"]
                if listing.get("photo_urls"):
                    extras["photo_urls"] = listing["photo_urls"]
                if extras:
                    db.update_listing_details(listing["id"], extras)

                # Bootstrap Zillow-detected price cuts into DB
                if listing.get("_price_change") and listing.get("price"):
                    bootstrapped = db.bootstrap_price_cut(
                        listing["id"],
                        listing["_price_change"],
                        listing["price"],
                        listing.get("_price_change_date"),
                    )
                    if bootstrapped:
                        pc = abs(listing["_price_change"])
                        pct = (pc / (listing["price"] + pc)) * 100
                        summary["price_cuts"] += 1
                        print(f"  ZILLOW PRICE CUT: {listing['address']} -${pc:,} ({pct:.1f}%)")

            except Exception as e:
                msg = f"Upsert failed for {listing.get('id', '?')}: {e}"
                print(f"[Tracker] ERROR: {msg}")
                summary["errors"].append(msg)

        # -------------------------------------------------------------------
        # Step 3: State transitions (missing → gone)
        # Guard: only run if we actually fetched listings from at least one source.
        # If both fetchers failed, seen_ids is empty and we'd falsely mark
        # everything as missing.
        # -------------------------------------------------------------------
        if not seen_ids:
            print(f"[Tracker] Skipping state transitions — no listings fetched (both sources may have failed)")
        else:
            try:
                grace = search.get("grace_days", DEFAULT_GRACE_DAYS)
                missing_count = db.mark_missing(market_id, seen_ids, grace)
                if missing_count:
                    print(f"[Tracker] Marked {missing_count} listings as missing")

                gone_count = db.expire_missing()
                if gone_count:
                    print(f"[Tracker] Expired {gone_count} listings to gone")
            except Exception as e:
                msg = f"State transition error: {e}"
                print(f"[Tracker] ERROR: {msg}")
                summary["errors"].append(msg)

    if dry_run:
        elapsed = time.time() - start
        print(f"\n[Tracker] DRY RUN complete in {elapsed:.1f}s")
        return summary

    # -----------------------------------------------------------------------
    # Step 4: Detail fetch (remarks, photos for new listings)
    # -----------------------------------------------------------------------
    if not skip_details:
        try:
            needs_detail = db.get_listings_needing_details(limit=50)
            if needs_detail:
                # Split by source — each uses its own detail fetcher
                rf_listings = [l for l in needs_detail if dict(l).get("source") == "redfin"]
                zl_listings = [l for l in needs_detail if dict(l).get("source") == "zillow"]

                if rf_listings:
                    print(f"\n[Tracker] Fetching Redfin details for {len(rf_listings)} listings...")
                    details = redfin_fetcher.fetch_details_batch(rf_listings)
                    for lid, detail in details.items():
                        if detail:
                            db.update_listing_details(lid, detail)
                            summary["details_fetched"] += 1

                if zl_listings:
                    print(f"\n[Tracker] Fetching Zillow details for {len(zl_listings)} listings...")
                    details = zillow_fetcher.fetch_details_batch(zl_listings)
                    for lid, detail in details.items():
                        if detail:
                            db.update_listing_details(lid, detail)
                            summary["details_fetched"] += 1
        except Exception as e:
            msg = f"Detail fetch error: {e}"
            print(f"[Tracker] ERROR: {msg}")
            summary["errors"].append(msg)

    # -----------------------------------------------------------------------
    # Step 4b: Description cross-reference via Redfin
    # For listings still missing remarks, find on Redfin by address
    # -----------------------------------------------------------------------
    if not skip_descriptions:
        try:
            missing_remarks = db.get_listings_missing_remarks(limit=50)
            if missing_remarks:
                print(f"\n[Tracker] Cross-referencing {len(missing_remarks)} listings for descriptions via Redfin...")
                descs = redfin_fetcher.fetch_descriptions_batch(
                    [dict(l) for l in missing_remarks]
                )
                for lid, detail in descs.items():
                    if detail and (
                        detail.get("remarks")
                        or detail.get("redfin_estimate")
                        or detail.get("assessed_value")
                        or detail.get("last_sold_price")
                        or detail.get("seller_phone")
                    ):
                        db.update_listing_remarks(
                            lid,
                            detail.get("remarks"),
                            redfin_url=detail.get("redfin_url"),
                            redfin_estimate=detail.get("redfin_estimate"),
                            assessed_value=detail.get("assessed_value"),
                            last_sold_price=detail.get("last_sold_price"),
                            last_sold_date=detail.get("last_sold_date"),
                            seller_name=detail.get("seller_name"),
                            seller_phone=detail.get("seller_phone"),
                            seller_email=detail.get("seller_email"),
                            seller_broker=detail.get("seller_broker"),
                        )
                        summary["descriptions_found"] += 1
        except Exception as e:
            msg = f"Description cross-reference error: {e}"
            print(f"[Tracker] ERROR: {msg}")
            summary["errors"].append(msg)

    # -----------------------------------------------------------------------
    # Step 5: Score all active listings
    # -----------------------------------------------------------------------
    try:
        active = db.get_active_listings()
        print(f"\n[Tracker] Scoring {len(active)} active listings...")
        for listing in active:
            score_result = scorer.score_listing(dict(listing))
            db.update_listing_score(
                listing["id"],
                score_result["total"],
                score_result["breakdown"],
                score_result["keywords_matched"],
            )
            summary["scored"] += 1
            if score_result["is_high_priority"]:
                summary["high_priority"] += 1
    except Exception as e:
        msg = f"Scoring error: {e}"
        print(f"[Tracker] ERROR: {msg}")
        summary["errors"].append(msg)

    # -----------------------------------------------------------------------
    # Step 5b: FEMA flood zone summary for all active listings
    # -----------------------------------------------------------------------
    try:
        active = db.get_active_listings()
        print(f"\n[Tracker] Refreshing FEMA flood zone for {len(active)} listings...")
        for listing in active:
            lat = listing.get("latitude")
            lon = listing.get("longitude")
            if lat is None or lon is None:
                continue
            summary["flood_checked"] += 1
            flood = lookup_flood_zone(float(lat), float(lon))
            if not flood:
                continue
            db.update_listing_flood(
                listing["id"],
                flood_zone=flood.get("zone"),
                flood_risk_level=flood.get("risk_level"),
            )
            summary["flood_updated"] += 1
    except Exception as e:
        msg = f"Flood zone refresh error: {e}"
        print(f"[Tracker] ERROR: {msg}")
        summary["errors"].append(msg)

    # -----------------------------------------------------------------------
    # Step 5c: NDVI vegetation check (90-day refresh or first-time only)
    # -----------------------------------------------------------------------
    if os.environ.get("NDVI_ENRICHMENT_ENABLED", "true").lower() in ("1", "true", "yes"):
        try:
            from .ndvi_lite import get_naip_ndvi
            active = db.get_active_listings()
            ndvi_candidates = [
                l for l in active
                if l.get("latitude") and l.get("longitude")
                and (
                    not l.get("ndvi_checked_at")
                    or (datetime.utcnow() - l["ndvi_checked_at"]).days >= 90
                )
            ]
            if ndvi_candidates:
                print(f"\n[Tracker] NDVI check for {len(ndvi_candidates)} listings...")
                for listing in ndvi_candidates:
                    try:
                        summary["ndvi_checked"] += 1
                        result = get_naip_ndvi(float(listing["latitude"]), float(listing["longitude"]))
                        if result:
                            db.update_listing_ndvi(listing["id"], **result)
                            summary["ndvi_updated"] += 1
                    except Exception as e:
                        print(f"[Tracker] NDVI failed for {listing['id']}: {e}")
                    time.sleep(1)  # Gentle pacing for government API
        except Exception as e:
            msg = f"NDVI enrichment error: {e}"
            print(f"[Tracker] ERROR: {msg}")
            summary["errors"].append(msg)

    # -----------------------------------------------------------------------
    # Step 6: Photo AI (conditional — only high-signal listings)
    # -----------------------------------------------------------------------
    if not skip_photos:
        try:
            candidates = db.get_listings_for_photo_ai()
            if candidates:
                print(f"\n[Tracker] Photo AI candidates: {len(candidates)}")
                for listing in candidates:
                    photos_raw = listing.get("photo_urls")
                    if isinstance(photos_raw, str):
                        try:
                            photos_raw = json.loads(photos_raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                    if not photos_raw:
                        continue

                    result = photo_analyzer.analyze_photos(photos_raw)
                    if result:
                        db.update_photo_analysis(listing["id"], result)
                        summary["photos_analyzed"] += 1
                        print(f"  Photo AI: {listing['id']} → damage:{result.get('damage_score', '?')}/10 ({result.get('estimated_work_level', '?')})")

                    time.sleep(1)  # Rate limit between AI calls

                # Re-score after photo analysis
                _rescore_photo_analyzed(candidates)
        except Exception as e:
            msg = f"Photo AI error: {e}"
            print(f"[Tracker] ERROR: {msg}")
            summary["errors"].append(msg)

    # -----------------------------------------------------------------------
    # Step 7: Export JSON for display UI
    # -----------------------------------------------------------------------
    try:
        _export_json(summary)
    except Exception as e:
        msg = f"JSON export error: {e}"
        print(f"[Tracker] ERROR: {msg}")
        summary["errors"].append(msg)

    elapsed = time.time() - start
    _print_summary(summary, elapsed)

    # Fire notification matching in background thread (off the pipeline hot path)
    try:
        from threading import Thread
        from .notification_service import match_and_dispatch
        Thread(target=match_and_dispatch, daemon=True).start()
        print("[Tracker] Notification matching started in background")
    except Exception as e:
        print(f"[Tracker] Warning: notification matching failed to start: {e}")

    return summary


def _rescore_photo_analyzed(listings: list):
    """Re-score listings that just got photo analysis."""
    try:
        full = db.get_active_listings()
        by_id = {row["id"]: row for row in full}
        for listing in listings:
            row = by_id.get(listing["id"])
            if row:
                result = scorer.score_listing(dict(row))
                db.update_listing_score(
                    row["id"], result["total"],
                    result["breakdown"], result["keywords_matched"],
                )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# JSON export for display UI
# ---------------------------------------------------------------------------
def _export_json(summary: dict):
    """Export active listings to JSON for the display UI."""
    active = db.get_active_listings(min_score=0)
    try:
        stats = db.get_tracker_stats()
    except Exception:
        stats = None

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "stats": dict(stats) if stats else {},
        "run_summary": {
            "total_fetched": summary["total_fetched"],
            "new": summary["new"],
            "price_cuts": summary["price_cuts"],
            "high_priority": summary["high_priority"],
            "descriptions_found": summary["descriptions_found"],
            "flood_checked": summary["flood_checked"],
            "flood_updated": summary["flood_updated"],
            "errors": len(summary["errors"]),
        },
        "listings": [],
    }

    for row in active:
        listing = dict(row)
        # Parse JSON fields
        for json_field in ("score_breakdown", "keywords_matched", "photo_urls", "photo_analysis_json"):
            val = listing.get(json_field)
            if isinstance(val, str):
                try:
                    listing[json_field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Convert datetime fields to ISO strings
        for dt_field in ("first_seen_at", "last_seen_at", "gone_at", "grace_until",
                         "last_price_cut_at", "photo_analyzed_at", "detail_fetched_at"):
            val = listing.get(dt_field)
            if isinstance(val, datetime):
                listing[dt_field] = val.isoformat() + "Z"

        output["listings"].append(listing)

    # Write to frontend directory for static fallback
    frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
    os.makedirs(frontend_dir, exist_ok=True)
    out_path = os.path.join(frontend_dir, "fsbo_latest.json")

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"[Tracker] Exported {len(output['listings'])} listings to {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_searches(market_ids: list = None) -> list:
    """Get search configs — from DB if available, else from config.py."""
    # Try DB first
    try:
        db_searches = db.get_active_searches()
        if db_searches:
            if market_ids:
                return [s for s in db_searches if s["id"] in market_ids]
            return list(db_searches)
    except Exception:
        pass

    # Fall back to config
    if market_ids:
        return [s for s in SEARCHES if s["id"] in market_ids]
    return SEARCHES


def _print_summary(summary: dict, elapsed: float):
    """Print run summary to stdout."""
    print(f"\n{'='*60}")
    print(f"[Tracker] Run complete in {elapsed:.1f}s")
    print(f"{'='*60}")
    print(f"  Markets:        {len(summary['markets'])}")
    print(f"  Total fetched:  {summary['total_fetched']}")
    print(f"  New listings:   {summary['new']}")
    print(f"  Updated:        {summary['updated']}")
    print(f"  Price cuts:     {summary['price_cuts']}")
    print(f"  Details:        {summary['details_fetched']}")
    print(f"  Descriptions:   {summary['descriptions_found']}")
    print(f"  Photos AI'd:    {summary['photos_analyzed']}")
    print(f"  Scored:         {summary['scored']}")
    print(f"  High priority:  {summary['high_priority']}")
    print(f"  Flood checked:  {summary['flood_checked']}")
    print(f"  Flood updated:  {summary['flood_updated']}")
    if summary["errors"]:
        print(f"  ERRORS:         {len(summary['errors'])}")
        for e in summary["errors"][:5]:
            print(f"    - {e}")
