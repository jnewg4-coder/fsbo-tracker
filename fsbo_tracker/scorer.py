"""
FSBO Listing Tracker — Scoring engine
Two layers: fast keyword regex scan + conditional photo AI scoring.
"""

import json
from .config import KEYWORDS, SCORE_CAPS, PRICE_RATIO_BANDS, DOM_BANDS, HIGH_PRIORITY_SCORE


def scan_keywords(remarks: str) -> tuple:
    """
    Scan remarks text for motivation/condition keywords.
    Returns (score, matched_keywords) where matched_keywords is a list of
    {"label": str, "tier": str, "points": int, "match": str} dicts.
    """
    if not remarks:
        return 0, []

    matched = []
    total = 0

    for kw in KEYWORDS:
        m = kw["pattern"].search(remarks)
        if m:
            matched.append({
                "label": kw["label"],
                "tier": kw["tier"],
                "points": kw["points"],
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
            })
            total += kw["points"]

    capped = min(total, SCORE_CAPS["keywords"])
    return capped, matched


def score_price_ratio(price: int, assessed_value: int, redfin_estimate: int) -> int:
    """
    Score based on ask price vs assessed value or Redfin estimate.
    Takes the BEST (highest scoring) of the two ratios.
    """
    best_score = 0

    for ref_value in [assessed_value, redfin_estimate]:
        if not ref_value or ref_value <= 0 or not price or price <= 0:
            continue
        ratio = price / ref_value
        for threshold, points in PRICE_RATIO_BANDS:
            if ratio < threshold:
                best_score = max(best_score, points)
                break

    return min(best_score, SCORE_CAPS["price_ratio"])


def score_dom(dom: int, days_seen: int) -> int:
    """Score based on days on market (use dom if available, else days_seen)."""
    effective_dom = dom if dom and dom > 0 else (days_seen or 0)

    for threshold, points in DOM_BANDS:
        if effective_dom >= threshold:
            return min(points, SCORE_CAPS["dom"])
    return 0


def score_price_cuts(price_cuts: int) -> int:
    """Score based on number of price reductions."""
    if price_cuts >= 2:
        return 5
    elif price_cuts >= 1:
        return 3
    return 0


def score_listing(listing: dict) -> dict:
    """
    Compute full score for a listing.
    Returns {
        "total": int,
        "breakdown": {"keywords": int, "photos": int, "price_ratio": int, "dom": int, "cuts": int},
        "keywords_matched": [...],
        "is_high_priority": bool,
    }
    """
    # Keywords (always computed)
    kw_score, kw_matched = scan_keywords(listing.get("remarks", ""))

    # Photo damage (only if already analyzed — not triggered here)
    photo_score = 0
    damage = listing.get("photo_damage_score")
    if damage is not None and damage >= 0:
        photo_score = min(round(damage * 2.5), SCORE_CAPS["photos"])

    # Price ratio
    price_score = score_price_ratio(
        listing.get("price", 0),
        listing.get("assessed_value", 0),
        listing.get("redfin_estimate", 0),
    )

    # DOM
    dom_score = score_dom(listing.get("dom", 0), listing.get("days_seen", 0))

    # Price cuts
    cuts_score = score_price_cuts(listing.get("price_cuts", 0))

    total = kw_score + photo_score + price_score + dom_score + cuts_score

    breakdown = {
        "keywords": kw_score,
        "photos": photo_score,
        "price_ratio": price_score,
        "dom": dom_score,
        "cuts": cuts_score,
    }

    return {
        "total": total,
        "breakdown": breakdown,
        "keywords_matched": kw_matched,
        "is_high_priority": total >= HIGH_PRIORITY_SCORE,
    }


def should_trigger_photo_ai(listing: dict, breakdown: dict) -> bool:
    """
    Determine if a listing qualifies for photo AI analysis.
    Triggered when OTHER signals already show motivation.
    """
    from .config import PHOTO_AI_TRIGGERS

    # Already analyzed
    if listing.get("photo_analyzed_at"):
        return False

    # No photos available
    photos = listing.get("photo_urls")
    if not photos:
        return False
    if isinstance(photos, str):
        try:
            photos = json.loads(photos)
        except (json.JSONDecodeError, TypeError):
            return False
    if not photos:
        return False

    # Check triggers
    if breakdown.get("keywords", 0) >= PHOTO_AI_TRIGGERS["min_keyword_score"]:
        return True

    price = listing.get("price", 0)
    assessed = listing.get("assessed_value", 0)
    if price > 0 and assessed > 0:
        if price / assessed <= PHOTO_AI_TRIGGERS["max_price_ratio"]:
            return True

    effective_dom = listing.get("dom") or listing.get("days_seen", 0)
    if effective_dom >= PHOTO_AI_TRIGGERS["min_dom_with_cuts"] and listing.get("price_cuts", 0) >= 1:
        return True

    # Long-stale listings (high DOM even without cuts)
    min_dom_no_cuts = PHOTO_AI_TRIGGERS.get("min_dom_no_cuts", 90)
    if effective_dom >= min_dom_no_cuts:
        return True

    return False
