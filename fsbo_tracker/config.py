"""
FSBO Listing Tracker — Configuration
Markets, keyword tiers, scoring weights. All tuneable.
"""

import re

# ---------------------------------------------------------------------------
# Market search configs (parsed from Redfin URLs)
# ---------------------------------------------------------------------------
SEARCHES = [
    # ── Original markets ─────────────────────────────────────────────
    {
        "id": "charlotte-nc",
        "name": "Charlotte-Concord-Gastonia NC-SC CBSA",
        "region_id": 3105,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Charlotte/Concord/Gastonia/Rock Hill/Fort Mill/Monroe/Huntersville/Kannapolis/Mooresville/Salisbury
        "max_lat": 35.65, "min_lat": 34.75,
        "max_lng": -80.25, "min_lng": -81.35,
    },
    {
        "id": "nashville-tn",
        "name": "Nashville-Davidson-Murfreesboro-Franklin TN CBSA",
        "region_id": 13415,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Nashville/Franklin/Murfreesboro/Hendersonville/Mt Juliet/Gallatin/Spring Hill/Lebanon/Dickson
        "max_lat": 36.60, "min_lat": 35.65,
        "max_lng": -86.10, "min_lng": -87.30,
    },
    {
        "id": "tampa-fl",
        "name": "Tampa-St Petersburg-Clearwater FL CBSA",
        "region_id": 18142,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Tampa/St Pete/Clearwater/Brandon/Riverview/Wesley Chapel/Plant City/Lakeland/Largo/Dunedin
        "max_lat": 28.75, "min_lat": 27.50,
        "max_lng": -81.75, "min_lng": -82.95,
    },
    {
        "id": "greensboro-nc",
        "name": "Greensboro-High Point NC CBSA",
        "region_id": 7161,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Greensboro/High Point/Kernersville/Burlington/Thomasville/Asheboro
        "max_lat": 36.55, "min_lat": 35.50,
        "max_lng": -79.25, "min_lng": -80.55,
    },
    {
        "id": "winston-salem-nc",
        "name": "Winston-Salem NC CBSA",
        "region_id": 19017,
        "max_price": 800_000,
        "min_beds": 0,
        # Same Triad bbox — different Redfin region captures WS-specific listings
        "max_lat": 36.55, "min_lat": 35.50,
        "max_lng": -79.25, "min_lng": -80.55,
    },
    {
        "id": "birmingham-al",
        "name": "Birmingham-Hoover AL CBSA",
        "region_id": 1823,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Birmingham/Hoover/Vestavia Hills/Trussville/Alabaster/Pelham/Bessemer/Calera
        "max_lat": 34.08, "min_lat": 33.10,
        "max_lng": -86.25, "min_lng": -87.45,
    },
    {
        "id": "little-rock-ar",
        "name": "Little Rock-North Little Rock-Conway AR CBSA",
        "region_id": 10455,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Little Rock/NLR/Conway/Benton/Bryant/Sherwood/Maumelle/Cabot/Jacksonville AR
        "max_lat": 35.25, "min_lat": 34.30,
        "max_lng": -91.75, "min_lng": -93.15,
    },
    {
        "id": "akron-oh",
        "name": "Akron OH CBSA",
        "region_id": 244,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Akron/Medina/Cuyahoga Falls/Stow/Hudson/Kent/Barberton/Wadsworth
        "max_lat": 41.35, "min_lat": 40.78,
        "max_lng": -81.00, "min_lng": -82.05,
    },
    # ── Phase 3b expansion ───────────────────────────────────────────
    {
        "id": "atlanta-ga",
        "name": "Atlanta-Sandy Springs-Alpharetta GA CBSA",
        "region_id": 30756,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Atlanta/Marietta/Roswell/Sandy Springs/Alpharetta/Kennesaw/Lawrenceville/Peachtree City/Douglasville/Woodstock
        "max_lat": 34.25, "min_lat": 33.30,
        "max_lng": -83.85, "min_lng": -84.90,
    },
    {
        "id": "jacksonville-fl",
        "name": "Jacksonville FL CBSA",
        "region_id": 8907,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Jacksonville/Orange Park/Fernandina Beach/St Augustine/Fleming Island/Ponte Vedra/Middleburg
        "max_lat": 30.65, "min_lat": 29.70,
        "max_lng": -81.10, "min_lng": -82.10,
    },
    {
        "id": "memphis-tn",
        "name": "Memphis TN-MS-AR CBSA",
        "region_id": 12260,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Memphis/Germantown/Collierville/Bartlett/Southaven/Olive Branch/West Memphis AR/Hernando MS
        "max_lat": 35.45, "min_lat": 34.75,
        "max_lng": -89.55, "min_lng": -90.35,
    },
    {
        "id": "indianapolis-in",
        "name": "Indianapolis-Carmel-Anderson IN CBSA",
        "region_id": 9170,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Indianapolis/Carmel/Fishers/Greenwood/Noblesville/Lawrence/Plainfield/Avon/Zionsville/Anderson
        "max_lat": 40.15, "min_lat": 39.45,
        "max_lng": -85.75, "min_lng": -86.55,
    },
    {
        "id": "columbus-oh",
        "name": "Columbus OH CBSA",
        "region_id": 4664,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Columbus/Dublin/Westerville/Reynoldsburg/Grove City/Hilliard/Gahanna/Delaware/Lancaster/Marysville
        "max_lat": 40.35, "min_lat": 39.65,
        "max_lng": -82.55, "min_lng": -83.40,
    },
    {
        "id": "san-antonio-tx",
        "name": "San Antonio-New Braunfels TX CBSA",
        "region_id": 16657,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: San Antonio/New Braunfels/Schertz/Cibolo/Live Oak/Converse/Seguin/Boerne/Canyon Lake
        "max_lat": 29.85, "min_lat": 29.10,
        "max_lng": -98.05, "min_lng": -98.95,
    },
    {
        "id": "lexington-ky",
        "name": "Lexington-Fayette KY CBSA",
        "region_id": 11746,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Lexington/Georgetown/Nicholasville/Versailles/Richmond/Winchester/Paris
        "max_lat": 38.25, "min_lat": 37.55,
        "max_lng": -84.05, "min_lng": -85.05,
    },
    {
        "id": "philadelphia-pa",
        "name": "Philadelphia-Camden-Wilmington PA-NJ-DE CBSA",
        "region_id": 15502,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA (focused): Philadelphia/King of Prussia/Cherry Hill/Media/Norristown/Wilmington DE/Camden NJ
        "max_lat": 40.25, "min_lat": 39.70,
        "max_lng": -74.90, "min_lng": -75.65,
    },
    # ── Wave 2 expansion (Feb 2026) ──────────────────────────────────
    {
        "id": "cleveland-oh",
        "name": "Cleveland-Elyria OH CBSA",
        "region_id": 4145,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Cleveland/Lakewood/Parma/Strongsville/Mentor/Euclid/Solon/Westlake/Elyria/Lorain
        "max_lat": 41.75, "min_lat": 41.10,
        "max_lng": -81.15, "min_lng": -82.20,
    },
    {
        "id": "raleigh-nc",
        "name": "Raleigh-Cary NC CBSA",
        "region_id": 35711,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Raleigh/Cary/Apex/Wake Forest/Garner/Holly Springs/Fuquay-Varina/Clayton/Knightdale/Wendell
        "max_lat": 36.10, "min_lat": 35.50,
        "max_lng": -78.40, "min_lng": -79.00,
    },
    {
        "id": "orlando-fl",
        "name": "Orlando-Kissimmee-Sanford FL CBSA",
        "region_id": 13655,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Orlando/Kissimmee/Sanford/Winter Park/Clermont/Altamonte Springs/Oviedo/Deltona/Daytona fringe
        "max_lat": 28.90, "min_lat": 28.05,
        "max_lng": -80.95, "min_lng": -81.90,
    },
    {
        "id": "houston-tx",
        "name": "Houston-The Woodlands-Sugar Land TX CBSA",
        "region_id": 8903,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA (focused): Houston/Sugar Land/Pearland/Katy/The Woodlands/Conroe/League City/Missouri City/Baytown
        "max_lat": 30.30, "min_lat": 29.30,
        "max_lng": -94.95, "min_lng": -96.00,
    },
    {
        "id": "st-louis-mo",
        "name": "St. Louis MO-IL CBSA",
        "region_id": 16661,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: St. Louis/Florissant/O'Fallon/Chesterfield/St. Charles/Belleville IL/Edwardsville IL
        "max_lat": 38.95, "min_lat": 38.25,
        "max_lng": -89.90, "min_lng": -91.00,
    },
    {
        "id": "kansas-city-mo",
        "name": "Kansas City MO-KS CBSA",
        "region_id": 35751,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: KC MO/Independence/Lee's Summit/Blue Springs/Overland Park KS/Olathe KS/Lenexa KS/Leavenworth KS
        "max_lat": 39.30, "min_lat": 38.65,
        "max_lng": -94.20, "min_lng": -95.05,
    },
    {
        "id": "pittsburgh-pa",
        "name": "Pittsburgh PA CBSA",
        "region_id": 15702,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Pittsburgh/Mt. Lebanon/Bethel Park/Cranberry Twp/Monroeville/McKeesport/Penn Hills/Wexford
        "max_lat": 40.75, "min_lat": 40.15,
        "max_lng": -79.60, "min_lng": -80.45,
    },
    {
        "id": "knoxville-tn",
        "name": "Knoxville TN CBSA",
        "region_id": 10200,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Knoxville/Maryville/Farragut/Oak Ridge/Sevierville/Lenoir City/Clinton/Loudon
        "max_lat": 36.20, "min_lat": 35.60,
        "max_lng": -83.55, "min_lng": -84.45,
    },
    {
        "id": "columbia-sc",
        "name": "Columbia SC CBSA",
        "region_id": 4149,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Columbia/Irmo/Lexington SC/Cayce/West Columbia/Blythewood/Chapin/Camden/Elgin
        "max_lat": 34.35, "min_lat": 33.70,
        "max_lng": -80.60, "min_lng": -81.40,
    },
    {
        "id": "chattanooga-tn",
        "name": "Chattanooga TN-GA CBSA",
        "region_id": 3641,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Chattanooga/East Ridge/Red Bank/Hixson/Signal Mountain/Soddy-Daisy/Fort Oglethorpe GA/Ringgold GA
        "max_lat": 35.30, "min_lat": 34.80,
        "max_lng": -85.00, "min_lng": -85.60,
    },
    {
        "id": "detroit-mi",
        "name": "Detroit-Warren-Dearborn MI CBSA",
        "region_id": 5665,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA: Detroit/Dearborn/Livonia/Sterling Heights/Warren/Troy/Royal Oak/Southfield/Ann Arbor fringe/Pontiac
        "max_lat": 42.75, "min_lat": 42.05,
        "max_lng": -82.75, "min_lng": -83.55,
    },
    {
        "id": "dallas-tx",
        "name": "Dallas-Fort Worth-Arlington TX CBSA",
        "region_id": 30794,
        "max_price": 800_000,
        "min_beds": 0,
        # CBSA (focused): Dallas/Plano/Frisco/Arlington/Irving/Richardson/Garland/McKinney/Denton/Fort Worth
        "max_lat": 33.30, "min_lat": 32.45,
        "max_lng": -96.45, "min_lng": -97.45,
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
DEFAULT_GRACE_DAYS = 7  # listings absent for 7 days → gone (3 was too aggressive with paginated scrapers)

# Rate limiting (seconds between requests)
REDFIN_DELAY = 2.0
ZILLOW_DELAY = 3.0
DETAIL_FETCH_DELAY = 3.0

# Inter-market pause: random delay between processing each market
# Prevents all markets from hammering sources in a tight burst
INTER_MARKET_DELAY_MIN = 5.0   # minimum seconds between markets
INTER_MARKET_DELAY_MAX = 15.0  # maximum seconds between markets
