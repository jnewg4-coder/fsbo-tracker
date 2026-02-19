"""
FSBO Listing Tracker — API Router

Endpoints for listing data, on-demand photo analysis, and geo enrichment.
All endpoints are admin-only (personal tool, not customer-facing).
Gated behind FSBO_ENABLED env var and admin auth.
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger("api.fsbo")

router = APIRouter()

# ---------------------------------------------------------------------------
# Auth: standalone admin verify (checks ADMIN_PASSWORD header)
# ---------------------------------------------------------------------------
from fastapi import Header


async def verify_fsbo_admin(x_admin_password: str = Header(...)):
    expected = os.getenv("ADMIN_PASSWORD", "")
    if not expected or x_admin_password != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SAFE_ERROR = "An internal error occurred"


def _serialize_listing(row: dict) -> dict:
    """Convert a DB row (RealDictRow) to JSON-safe dict."""
    out = dict(row)
    for key in ("score_breakdown", "keywords_matched", "photo_urls", "photo_analysis_json"):
        val = out.get(key)
        if isinstance(val, str):
            try:
                out[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    for key in ("first_seen_at", "last_seen_at", "gone_at", "grace_until",
                "last_price_cut_at", "photo_analyzed_at", "detail_fetched_at", "created_at"):
        val = out.get(key)
        if isinstance(val, datetime):
            out[key] = val.isoformat()
    return out


# ---------------------------------------------------------------------------
# Endpoints (all require admin auth)
# ---------------------------------------------------------------------------
@router.get("/fsbo/listings")
async def get_listings(
    _admin: bool = Depends(verify_fsbo_admin),
    search_id: Optional[str] = Query(None, description="Filter by market search ID"),
    min_score: int = Query(0, description="Minimum score filter"),
    limit: int = Query(500, description="Max listings to return", le=2000),
    include_gone: bool = Query(False, description="Include gone/expired listings"),
):
    """Get all active/watched FSBO listings with stats."""
    from fsbo_tracker.db import get_active_listings, get_tracker_stats, db_cursor

    try:
        listings = get_active_listings(search_id=search_id, min_score=min_score)
        listings = list(listings)[:limit]

        if include_gone:
            with db_cursor(commit=False) as (conn, cur):
                cur.execute(
                    "SELECT * FROM fsbo_listings WHERE status = 'gone' ORDER BY gone_at DESC LIMIT 50"
                )
                gone = cur.fetchall()
                listings = listings + list(gone)

        stats = get_tracker_stats()

        return {
            "listings": [_serialize_listing(l) for l in listings],
            "stats": dict(stats) if stats else {},
            "generated_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"[FSBO] get_listings error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)


@router.get("/fsbo/listings/{listing_id}")
async def get_listing_detail(listing_id: str, _admin: bool = Depends(verify_fsbo_admin)):
    """Get a single listing with full detail + price history."""
    from fsbo_tracker.db import db_cursor, get_price_history

    try:
        with db_cursor(commit=False) as (conn, cur):
            cur.execute("SELECT * FROM fsbo_listings WHERE id = %s", (listing_id,))
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Listing not found")

        listing = _serialize_listing(row)
        listing["price_history"] = [
            {**dict(e), "detected_at": e["detected_at"].isoformat() if isinstance(e["detected_at"], datetime) else e["detected_at"]}
            for e in get_price_history(listing_id)
        ]

        return listing
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FSBO] get_listing_detail error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)


@router.post("/fsbo/listings/{listing_id}/analyze-photos")
async def analyze_listing_photos(listing_id: str, _admin: bool = Depends(verify_fsbo_admin)):
    """
    Trigger Claude Haiku vision analysis on a listing's photos.
    On-demand — works on ANY listing regardless of score.
    Returns the analysis result and updated score.
    """
    from fsbo_tracker.db import db_cursor, update_photo_analysis, update_listing_score
    from fsbo_tracker.photo_analyzer import analyze_photos
    from fsbo_tracker.scorer import score_listing

    try:
        with db_cursor(commit=False) as (conn, cur):
            cur.execute(
                "SELECT id, photo_urls, remarks, price, assessed_value, redfin_estimate, "
                "dom, days_seen, price_cuts, photo_damage_score "
                "FROM fsbo_listings WHERE id = %s",
                (listing_id,),
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Listing not found")

        photo_urls = row.get("photo_urls")
        if isinstance(photo_urls, str):
            try:
                photo_urls = json.loads(photo_urls)
            except (json.JSONDecodeError, TypeError):
                photo_urls = []

        if not photo_urls:
            raise HTTPException(status_code=400, detail="No photos available for this listing")

        analysis = analyze_photos(photo_urls)
        if not analysis:
            raise HTTPException(status_code=502, detail="Photo analysis failed")

        update_photo_analysis(listing_id, analysis)

        listing_dict = dict(row)
        listing_dict["photo_damage_score"] = analysis.get("damage_score", 0)
        score_result = score_listing(listing_dict)
        update_listing_score(
            listing_id,
            score_result["total"],
            score_result["breakdown"],
            score_result["keywords_matched"],
        )

        return {
            "listing_id": listing_id,
            "analysis": analysis,
            "new_score": score_result["total"],
            "new_breakdown": score_result["breakdown"],
            "is_high_priority": score_result["is_high_priority"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FSBO] analyze_photos error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)


@router.post("/fsbo/listings/{listing_id}/geo-enrich")
async def geo_enrich_listing(listing_id: str, _admin: bool = Depends(verify_fsbo_admin)):
    """
    Run geo proximity analysis for a listing.
    Uses standalone geo_lite module (HIFLD + EPA + FEMA public APIs).
    """
    from fsbo_tracker.db import db_cursor
    from fsbo_tracker.geo_lite import enrich

    try:
        with db_cursor(commit=False) as (conn, cur):
            cur.execute(
                "SELECT id, address, latitude, longitude FROM fsbo_listings WHERE id = %s",
                (listing_id,),
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Listing not found")

        lat, lon = row.get("latitude"), row.get("longitude")
        if not lat or not lon:
            raise HTTPException(status_code=400, detail="Listing has no coordinates")

        result = enrich(float(lat), float(lon))

        return {
            "listing_id": listing_id,
            "success": result.get("success", False),
            "layers_queried": result.get("layers_queried", 0),
            "layers_succeeded": result.get("layers_succeeded", 0),
            "total_adjustment_pct": result.get("total_adjustment_pct", 0),
            "risk_level": result.get("risk_level", "MINIMAL"),
            "risk_flags": result.get("risk_flags", []),
            "factors": result.get("factors", []),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FSBO] geo_enrich error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)


@router.get("/fsbo/searches")
async def get_searches(_admin: bool = Depends(verify_fsbo_admin)):
    """Get all configured market searches."""
    from fsbo_tracker.db import get_active_searches

    try:
        searches = get_active_searches()
        return {"searches": [dict(s) for s in searches]}
    except Exception as e:
        logger.error(f"[FSBO] get_searches error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)
