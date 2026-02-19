"""
FSBO Listing Tracker — Configuration
Markets, keyword tiers, scoring weights. All tuneable.
"""

import re

# ---------------------------------------------------------------------------
# Market search configs (parsed from Redfin URLs)
# ---------------------------------------------------------------------------
SEARCHES = [
    {
        "id": "charlotte-nc",
        "name": "Charlotte NC MSA",
        "region_id": 3105,
        "max_price": 500_000,
        "min_beds": 0,   # No bed filter on fetch — score handles it
        # Wide MSA bbox: Charlotte/Concord/Gastonia/Rock Hill/Fort Mill/Indian Trail/Monroe/Huntersville/Kannapolis
        "max_lat": 35.58, "min_lat": 34.88,
        "max_lng": -80.38, "min_lng": -81.25,
    },
    {
        "id": "nashville-tn",
        "name": "Nashville TN MSA",
        "region_id": 13415,
        "max_price": 600_000,
        "min_beds": 0,
        # Wider MSA bbox: includes White House, Hendersonville, Mt Juliet, Franklin, Spring Hill
        "max_lat": 36.55, "min_lat": 35.75,
        "max_lng": -86.25, "min_lng": -87.15,
    },
]

# ---------------------------------------------------------------------------
# Keyword tiers — regex patterns, case-insensitive
# Each entry: (compiled_regex, display_label, tier, points)
# ---------------------------------------------------------------------------
_KEYWORD_DEFS = [
    # Tier A — Hard motivation (7 pts each)
    (r"motivat",                                         "motivated",        "A", 7),
    (r"must sell|need(?:s)? to sell|has to sell",         "must sell",        "A", 7),
    (r"as[\s\-.]is|sold as[\s\-.]is",                    "as-is",            "A", 7),
    (r"estate sale|probate|deceased|inherited",           "estate/probate",   "A", 7),
    (r"foreclos|pre[\s\-.]?foreclos|bank[\s\-.]?owned|(?<!\w)REO(?!\w)", "foreclosure/REO", "A", 7),
    (r"price(?:d)? (?:reduc|cut)|just reduced|new price",  "price reduced",    "A", 7),
    (r"below (?:appraisal|market|assessed|value)",       "below market",     "A", 7),
    # Tier B — Soft motivation (4 pts each)
    (r"relocat|job transfer|transferred",                "relocation",       "B", 4),
    (r"divorc|settlement|separation",                    "divorce",          "B", 4),
    (r"bring.*(?:all|any).*offer|all offers|make.*offer","bring offers",     "B", 4),
    (r"seller financ|owner financ|creative financ",      "seller financing", "B", 4),
    (r"priced to sell|won['\u2019]?t last|will not last","priced to sell",   "B", 4),
    (r"downsiz|retirement|health reason|medical",        "downsizing",       "B", 4),
    # Tier C — Condition / opportunity (2 pts each)
    (r"fixer|fixer[\s\-.]?upper",                        "fixer",            "C", 2),
    (r"\bTLC\b|needs? TLC",                              "TLC",              "C", 2),
    (r"handyman|handy[\s\-.]?man",                       "handyman",         "C", 2),
    (r"needs? (?:work|updat|renovat|repair)",            "needs work",       "C", 2),
    (r"investor (?:special|opportun)",                   "investor special", "C", 2),
    (r"cash only|cash prefer|cash buyer",                "cash only",        "C", 2),
    (r"vacant|vacated|unoccupied",                       "vacant",           "C", 2),
    (r"short sale|wholesale",                            "short sale",       "C", 2),
    (r"deferred mainten|cosmetic|great potential",       "deferred maint",   "C", 2),
]

# Pre-compile all patterns
KEYWORDS = [
    {
        "pattern": re.compile(pat, re.IGNORECASE),
        "label": label,
        "tier": tier,
        "points": pts,
    }
    for pat, label, tier, pts in _KEYWORD_DEFS
]

# ---------------------------------------------------------------------------
# Scoring weights & thresholds
# ---------------------------------------------------------------------------
SCORE_CAPS = {
    "keywords":    40,
    "photos":      25,
    "price_ratio": 20,
    "dom":         10,
    "cuts":         5,
}

# Price-to-value ratio scoring bands
PRICE_RATIO_BANDS = [
    (0.80, 20),   # ask/assessed < 0.80 → 20 pts
    (0.90, 12),   # 0.80–0.90 → 12 pts
    (1.00,  6),   # 0.90–1.00 →  6 pts
]

# DOM scoring bands
DOM_BANDS = [
    (90, 10),
    (55,  6),
    (30,  3),
]

# Photo AI trigger thresholds
# Lowered since Zillow doesn't return full descriptions — trigger on DOM/price/cuts signals
PHOTO_AI_TRIGGERS = {
    "min_keyword_score":  5,     # Lower: flex text gives limited keyword matches
    "max_price_ratio":    0.95,  # Wider: trigger on any below-assessed listing
    "min_dom_with_cuts":  30,    # Lower: 30d + 1 cut is enough signal
    "min_dom_no_cuts":    90,    # NEW: long-stale listings even without cuts
}

# Shortlist thresholds (adjusted for limited keyword data — no full descriptions available)
SHORTLIST_MIN_SCORE = 15
HIGH_PRIORITY_SCORE = 30

# State management
DEFAULT_GRACE_DAYS = 3

# Rate limiting (seconds between requests)
REDFIN_DELAY = 2.0
ZILLOW_DELAY = 3.0
DETAIL_FETCH_DELAY = 3.0
