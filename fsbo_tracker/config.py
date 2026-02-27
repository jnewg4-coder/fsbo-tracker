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
    {
        "id": "tampa-fl",
        "name": "Tampa FL MSA",
        "region_id": 18142,
        "max_price": 500_000,
        "min_beds": 0,
        # Tampa/St Pete/Clearwater/Brandon/Riverview/Wesley Chapel/Plant City/Lakeland
        "max_lat": 28.70, "min_lat": 27.57,
        "max_lng": -81.85, "min_lng": -82.90,
    },
    {
        "id": "greensboro-nc",
        "name": "Greensboro NC (Piedmont Triad)",
        "region_id": 7161,
        "max_price": 400_000,
        "min_beds": 0,
        # Shared Triad bbox: Greensboro/Winston-Salem/High Point/Kernersville/Burlington
        "max_lat": 36.55, "min_lat": 35.50,
        "max_lng": -79.25, "min_lng": -80.55,
    },
    {
        "id": "winston-salem-nc",
        "name": "Winston-Salem NC (Piedmont Triad)",
        "region_id": 19017,
        "max_price": 400_000,
        "min_beds": 0,
        # Same Triad bbox — different Redfin region captures WS-specific listings
        "max_lat": 36.55, "min_lat": 35.50,
        "max_lng": -79.25, "min_lng": -80.55,
    },
    {
        "id": "birmingham-al",
        "name": "Birmingham AL MSA",
        "region_id": 1823,
        "max_price": 400_000,
        "min_beds": 0,
        # Birmingham/Hoover/Vestavia Hills/Homewood/Trussville/Alabaster
        "max_lat": 34.08, "min_lat": 33.10,
        "max_lng": -86.25, "min_lng": -87.45,
    },
    {
        "id": "little-rock-ar",
        "name": "Little Rock AR MSA",
        "region_id": 10455,
        "max_price": 400_000,
        "min_beds": 0,
        # Little Rock/NLR/Conway/Benton/Bryant/Sherwood/Maumelle/Cabot
        "max_lat": 35.25, "min_lat": 34.30,
        "max_lng": -91.75, "min_lng": -93.15,
    },
    {
        "id": "akron-oh",
        "name": "Akron / Medina / Cuyahoga Falls OH",
        "region_id": 244,
        "max_price": 350_000,
        "min_beds": 0,
        # Akron/Medina/Cuyahoga Falls/Stow/Hudson/Kent/Barberton
        "max_lat": 41.35, "min_lat": 40.78,
        "max_lng": -81.00, "min_lng": -82.05,
    },
    # ── Phase 3b expansion (Feb 2026) ────────────────────────────
    {
        "id": "atlanta-ga",
        "name": "Atlanta GA MSA",
        "region_id": 30756,
        "max_price": 500_000,
        "min_beds": 0,
        # Atlanta/Marietta/Roswell/Sandy Springs/Decatur/Kennesaw/Lawrenceville/Alpharetta
        "max_lat": 34.10, "min_lat": 33.45,
        "max_lng": -84.00, "min_lng": -84.75,
    },
    {
        "id": "jacksonville-fl",
        "name": "Jacksonville FL MSA",
        "region_id": 8907,
        "max_price": 450_000,
        "min_beds": 0,
        # Jacksonville/Orange Park/Fernandina Beach/St Augustine/Fleming Island
        "max_lat": 30.60, "min_lat": 29.80,
        "max_lng": -81.20, "min_lng": -82.05,
    },
    {
        "id": "memphis-tn",
        "name": "Memphis TN MSA",
        "region_id": 12260,
        "max_price": 350_000,
        "min_beds": 0,
        # Memphis/Germantown/Collierville/Bartlett/Southaven/Olive Branch
        "max_lat": 35.40, "min_lat": 34.85,
        "max_lng": -89.60, "min_lng": -90.25,
    },
    {
        "id": "indianapolis-in",
        "name": "Indianapolis IN MSA",
        "region_id": 9170,
        "max_price": 400_000,
        "min_beds": 0,
        # Indianapolis/Carmel/Fishers/Greenwood/Noblesville/Lawrence/Plainfield
        "max_lat": 39.98, "min_lat": 39.55,
        "max_lng": -85.90, "min_lng": -86.45,
    },
    {
        "id": "columbus-oh",
        "name": "Columbus OH MSA",
        "region_id": 4664,
        "max_price": 400_000,
        "min_beds": 0,
        # Columbus/Dublin/Westerville/Reynoldsburg/Grove City/Hilliard/Gahanna
        "max_lat": 40.20, "min_lat": 39.75,
        "max_lng": -82.65, "min_lng": -83.30,
    },
    {
        "id": "san-antonio-tx",
        "name": "San Antonio TX MSA",
        "region_id": 16657,
        "max_price": 450_000,
        "min_beds": 0,
        # San Antonio/New Braunfels/Schertz/Cibolo/Live Oak/Converse/Universal City
        "max_lat": 29.75, "min_lat": 29.20,
        "max_lng": -98.15, "min_lng": -98.85,
    },
    {
        "id": "lexington-ky",
        "name": "Lexington KY MSA",
        "region_id": 11746,
        "max_price": 400_000,
        "min_beds": 0,
        # Lexington/Georgetown/Nicholasville/Versailles/Richmond
        "max_lat": 38.18, "min_lat": 37.60,
        "max_lng": -84.10, "min_lng": -84.95,
    },
    {
        "id": "philadelphia-pa",
        "name": "Philadelphia PA Metro",
        "region_id": 15502,
        "max_price": 500_000,
        "min_beds": 0,
        # Philadelphia/King of Prussia/Cherry Hill/Media (focused metro, not full CSA)
        "max_lat": 40.18, "min_lat": 39.83,
        "max_lng": -74.97, "min_lng": -75.55,
    },
]

# Derived — use this everywhere instead of hardcoding market count
TOTAL_MARKETS = len(SEARCHES)

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

# Inter-market pause: random delay between processing each market
# Prevents 14 markets from hammering sources in a tight burst
INTER_MARKET_DELAY_MIN = 5.0   # minimum seconds between markets
INTER_MARKET_DELAY_MAX = 15.0  # maximum seconds between markets
