"""
FSBO Listing Tracker — CLI entry point

Usage:
    python -m fsbo_tracker                           # Full daily run
    python -m fsbo_tracker --dry-run                 # Fetch + parse, no DB writes
    python -m fsbo_tracker --migrate-only            # Run migration only
    python -m fsbo_tracker --market charlotte-nc     # Single market
    python -m fsbo_tracker --skip-zillow             # Skip Zillow
    python -m fsbo_tracker --skip-photos             # Skip photo AI
    python -m fsbo_tracker --score-only              # Re-score existing listings
    python -m fsbo_tracker --analyze-photos <id>     # On-demand photo AI
"""

import argparse
import json
import sys
import os

# Ensure package imports work when run from avm_platform root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    parser = argparse.ArgumentParser(
        description="FSBO Listing Tracker — Deal Discovery Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--migrate-only", action="store_true",
                        help="Run DB migration and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse without writing to DB")
    parser.add_argument("--market", type=str, action="append",
                        help="Market ID(s) to process (can repeat)")
    parser.add_argument("--skip-redfin", action="store_true",
                        help="Skip Redfin fetch")
    parser.add_argument("--skip-zillow", action="store_true",
                        help="Skip Zillow fetch")
    parser.add_argument("--skip-details", action="store_true",
                        help="Skip detail fetch pass")
    parser.add_argument("--skip-descriptions", action="store_true",
                        help="Skip Redfin description cross-reference")
    parser.add_argument("--skip-photos", action="store_true",
                        help="Skip photo AI analysis")
    parser.add_argument("--descriptions-only", action="store_true",
                        help="Only fetch descriptions for listings missing remarks")
    parser.add_argument("--score-only", action="store_true",
                        help="Re-score all active listings (no fetch)")
    parser.add_argument("--analyze-photos", type=str, metavar="LISTING_ID",
                        help="Run photo AI on a specific listing")
    parser.add_argument("--seed-searches", action="store_true",
                        help="Insert default search configs into DB")
    parser.add_argument("--stats", action="store_true",
                        help="Show tracker stats and exit")

    args = parser.parse_args()

    # Check for FSBO-dedicated DB connection (never fall back to shared AVMLens DB)
    if not os.environ.get("FSBO_DATABASE_URL"):
        print("ERROR: FSBO_DATABASE_URL is not set")
        print("FSBO tracker requires its own database to avoid convolving with AVMLens:")
        print("  export FSBO_DATABASE_URL='postgresql://...'")
        sys.exit(1)

    from . import db
    from .config import SEARCHES

    # --migrate-only
    if args.migrate_only:
        print("[Run] Running migration...")
        db.run_migration()
        return

    # --seed-searches
    if args.seed_searches:
        print("[Run] Seeding search configs...")
        for s in SEARCHES:
            db.upsert_search(s)
            print(f"  {s['id']}: {s['name']}")
        print("[Run] Done")
        return

    # --stats
    if args.stats:
        stats = db.get_tracker_stats()
        if stats:
            print("\nFSBO Tracker Stats:")
            print(f"  Active:        {stats.get('active_count', 0)}")
            print(f"  Watched:       {stats.get('watched_count', 0)}")
            print(f"  High priority: {stats.get('high_priority', 0)}")
            print(f"  New (3d):      {stats.get('new_recent', 0)}")
            print(f"  Recent cuts:   {stats.get('recent_cuts', 0)}")
        else:
            print("No stats available (run migration first?)")
        return

    # --analyze-photos <listing_id>
    if args.analyze_photos:
        _run_photo_analysis(args.analyze_photos)
        return

    # --score-only
    if args.score_only:
        _run_score_only()
        return

    # --descriptions-only
    if args.descriptions_only:
        _run_descriptions_only()
        return

    # Full daily run
    from . import tracker

    summary = tracker.run_daily(
        markets=args.market,
        skip_redfin=args.skip_redfin,
        skip_zillow=args.skip_zillow,
        skip_details=args.skip_details,
        skip_descriptions=args.skip_descriptions,
        skip_photos=args.skip_photos,
        dry_run=args.dry_run,
    )

    # Exit with error code if there were critical errors
    if summary.get("errors") and summary.get("total_fetched", 0) == 0:
        sys.exit(1)


def _run_photo_analysis(listing_id: str):
    """On-demand photo analysis for a single listing."""
    from . import db, photo_analyzer, scorer

    print(f"[Run] Analyzing photos for {listing_id}...")

    # Get listing
    with db.db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT * FROM fsbo_listings WHERE id = %s", (listing_id,))
        listing = cur.fetchone()

    if not listing:
        print(f"ERROR: Listing {listing_id} not found")
        sys.exit(1)

    photos_raw = listing.get("photo_urls")
    if isinstance(photos_raw, str):
        try:
            photos_raw = json.loads(photos_raw)
        except (json.JSONDecodeError, TypeError):
            print("ERROR: No valid photo URLs")
            sys.exit(1)

    if not photos_raw:
        print("ERROR: No photos available for this listing")
        sys.exit(1)

    result = photo_analyzer.analyze_photos(photos_raw)
    if result:
        db.update_photo_analysis(listing_id, result)
        print(f"\nPhoto Analysis Results:")
        print(f"  Damage score: {result.get('damage_score', '?')}/10")
        print(f"  Work level:   {result.get('estimated_work_level', '?')}")
        print(f"  Notes:        {result.get('damage_notes', '')}")
        if result.get("major_work_items"):
            print(f"  Work items:   {', '.join(result['major_work_items'])}")
        if result.get("red_flags"):
            print(f"  Red flags:    {', '.join(result['red_flags'])}")

        # Re-score
        updated = dict(listing)
        updated["photo_damage_score"] = result["damage_score"]
        score_result = scorer.score_listing(updated)
        db.update_listing_score(
            listing_id, score_result["total"],
            score_result["breakdown"], score_result["keywords_matched"],
        )
        print(f"  New score:    {score_result['total']} ({json.dumps(score_result['breakdown'])})")
    else:
        print("Photo analysis failed")
        sys.exit(1)


def _run_descriptions_only():
    """Fetch descriptions via Redfin cross-reference for listings missing remarks, then re-score."""
    from . import db, redfin_fetcher, scorer

    missing = db.get_listings_missing_remarks(limit=50)
    if not missing:
        print("[Run] All active listings already have descriptions")
        return

    print(f"[Run] {len(missing)} listings need descriptions — cross-referencing via Redfin...")
    descs = redfin_fetcher.fetch_descriptions_batch([dict(l) for l in missing])

    found = 0
    for lid, detail in descs.items():
        if detail and detail.get("remarks"):
            db.update_listing_remarks(
                lid,
                detail["remarks"],
                redfin_url=detail.get("redfin_url"),
                redfin_estimate=detail.get("redfin_estimate"),
                assessed_value=detail.get("assessed_value"),
            )
            found += 1

    print(f"\n[Run] Descriptions found: {found}/{len(missing)}")

    # Re-score all active listings with new data
    if found > 0:
        print("[Run] Re-scoring active listings with new descriptions...")
        active = db.get_active_listings()
        high = 0
        for listing in active:
            result = scorer.score_listing(dict(listing))
            db.update_listing_score(
                listing["id"], result["total"],
                result["breakdown"], result["keywords_matched"],
            )
            if result["is_high_priority"]:
                high += 1
        print(f"[Run] Scored {len(active)} listings, {high} high-priority")


def _run_score_only():
    """Re-score all active listings without fetching."""
    from . import db, scorer

    active = db.get_active_listings()
    print(f"[Run] Re-scoring {len(active)} active listings...")

    high = 0
    for listing in active:
        result = scorer.score_listing(dict(listing))
        db.update_listing_score(
            listing["id"], result["total"],
            result["breakdown"], result["keywords_matched"],
        )
        if result["is_high_priority"]:
            high += 1

    print(f"[Run] Scored {len(active)} listings, {high} high-priority")


if __name__ == "__main__":
    main()
