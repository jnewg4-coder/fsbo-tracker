"""
FSBO Listing Tracker — API Router

Endpoints for listing data, on-demand photo analysis, and geo enrichment.
Supports both JWT auth and legacy X-Admin-Password header during migration.
"""

import json
import logging
import math
import os
from datetime import datetime
from typing import Optional

import requests as http_requests

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .auth_router import get_current_user_or_admin

logger = logging.getLogger("api.fsbo")

router = APIRouter()


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
                "last_price_cut_at", "photo_analyzed_at", "detail_fetched_at",
                "created_at", "status_changed_at", "ndvi_checked_at"):
        val = out.get(key)
        if isinstance(val, datetime):
            out[key] = val.isoformat()
    # Date fields
    for key in ("sold_date",):
        val = out.get(key)
        if hasattr(val, "isoformat"):
            out[key] = val.isoformat()
    return out


# ---------------------------------------------------------------------------
# Endpoints (all require admin auth)
# ---------------------------------------------------------------------------
@router.get("/fsbo/listings")
async def get_listings(
    _user: dict = Depends(get_current_user_or_admin),
    search_id: Optional[str] = Query(None, description="Filter by market search ID"),
    min_score: int = Query(0, description="Minimum score filter"),
    limit: int = Query(500, description="Max listings to return", le=2000),
    include_gone: bool = Query(False, description="Include gone/expired listings"),
    include_sold: bool = Query(False, description="Include sold/closed listings"),
    ndvi_level: Optional[str] = Query(None, description="Filter by NDVI overgrowth level (HIGH, MODERATE, LOW, MINIMAL)"),
):
    """Get all active/watched/under_contract FSBO listings with stats."""
    from fsbo_tracker.db import get_active_listings, get_sold_listings, get_tracker_stats, db_cursor

    try:
        listings = get_active_listings(search_id=search_id, min_score=min_score)
        listings = list(listings)

        if ndvi_level:
            ndvi_upper = ndvi_level.upper()
            listings = [l for l in listings if (l.get("ndvi_overgrowth_level") or "").upper() == ndvi_upper]

        listings = listings[:limit]

        if include_gone:
            with db_cursor(commit=False) as (conn, cur):
                cur.execute(
                    "SELECT * FROM fsbo_listings WHERE status = 'gone' ORDER BY gone_at DESC LIMIT 50"
                )
                gone = cur.fetchall()
                listings = listings + list(gone)

        if include_sold:
            sold = get_sold_listings(search_id=search_id, limit=100)
            listings = listings + list(sold)

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
async def get_listing_detail(listing_id: str, _user: dict = Depends(get_current_user_or_admin)):
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
async def analyze_listing_photos(listing_id: str, _user: dict = Depends(get_current_user_or_admin)):
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

        remarks = row.get("remarks") or ""
        analysis = analyze_photos(photo_urls, remarks=remarks)
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
async def geo_enrich_listing(listing_id: str, _user: dict = Depends(get_current_user_or_admin)):
    """
    Run geo proximity analysis for a listing.
    Uses standalone geo_lite module (HIFLD + EPA + FEMA public APIs).
    """
    from fsbo_tracker.db import db_cursor, update_listing_flood
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
        flood_summary = result.get("flood_summary")
        if flood_summary:
            update_listing_flood(
                listing_id,
                flood_zone=flood_summary.get("zone"),
                flood_risk_level=flood_summary.get("risk_level"),
            )

        return {
            "listing_id": listing_id,
            "success": result.get("success", False),
            "layers_queried": result.get("layers_queried", 0),
            "layers_succeeded": result.get("layers_succeeded", 0),
            "total_adjustment_pct": result.get("total_adjustment_pct", 0),
            "risk_level": result.get("risk_level", "MINIMAL"),
            "risk_flags": result.get("risk_flags", []),
            "factors": result.get("factors", []),
            "flood_zone": flood_summary.get("zone") if flood_summary else None,
            "flood_risk_level": flood_summary.get("risk_level") if flood_summary else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FSBO] geo_enrich error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)


@router.get("/fsbo/searches")
async def get_searches(_user: dict = Depends(get_current_user_or_admin)):
    """Get all configured market searches."""
    from fsbo_tracker.db import get_active_searches

    try:
        searches = get_active_searches()
        return {"searches": [dict(s) for s in searches]}
    except Exception as e:
        logger.error(f"[FSBO] get_searches error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_SAFE_ERROR)


@router.post("/fsbo/run-pipeline")
async def trigger_pipeline(_user: dict = Depends(get_current_user_or_admin)):
    """
    Trigger a full pipeline run (fetch + score + export).
    Runs in a background thread to avoid blocking the API.
    Returns immediately with a status message.
    """
    import threading
    from fsbo_tracker.tracker import run_daily

    # Prevent concurrent runs
    if getattr(trigger_pipeline, '_running', False):
        return {"status": "already_running", "message": "Pipeline is already running"}

    def _run():
        try:
            trigger_pipeline._running = True
            logger.info("[FSBO] Pipeline triggered via API")
            summary = run_daily()
            logger.info(f"[FSBO] Pipeline complete: {summary.get('total_fetched', 0)} listings")
        except Exception as e:
            logger.error(f"[FSBO] Pipeline error: {e}", exc_info=True)
        finally:
            trigger_pipeline._running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "message": "Pipeline running in background"}


@router.get("/fsbo/proxy-status")
async def proxy_status(_user: dict = Depends(get_current_user_or_admin)):
    """Get current proxy session status."""
    from fsbo_tracker.proxy import get_status
    return get_status()


# ---------------------------------------------------------------------------
# Street View heading (free Metadata API → compute bearing to property)
# ---------------------------------------------------------------------------

def _get_street_view_heading(lat: float, lng: float, api_key: str):
    """Call free Street View Metadata API, compute heading from road panorama to property."""
    try:
        resp = http_requests.get(
            "https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": f"{lat},{lng}", "key": api_key},
            timeout=5,
        )
        data = resp.json()
        if data.get("status") != "OK":
            return None
        pano_lat = data["location"]["lat"]
        pano_lng = data["location"]["lng"]
        lat1, lat2 = math.radians(pano_lat), math.radians(lat)
        d_lng = math.radians(lng - pano_lng)
        x = math.sin(d_lng) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)
        heading = (math.degrees(math.atan2(x, y)) + 360) % 360
        return {"heading": round(heading, 1), "pano_lat": pano_lat, "pano_lng": pano_lng}
    except Exception as e:
        logger.error(f"[FSBO] Street View metadata error: {e}")
        return None


@router.get("/fsbo/street-view-heading")
async def get_sv_heading(lat: float, lng: float):
    """Get Street View heading that faces the property. Uses free Metadata API."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return {"heading": None}
    result = _get_street_view_heading(lat, lng, api_key)
    return result or {"heading": None}


@router.get("/fsbo/maps-key")
async def get_maps_key():
    """Return the browser-safe Maps Embed API key."""
    return {"key": os.environ.get("GOOGLE_MAPS_BROWSER_KEY", os.environ.get("GOOGLE_MAPS_API_KEY", ""))}
