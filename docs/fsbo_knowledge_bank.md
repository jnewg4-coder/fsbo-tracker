# FSBO Listing Tracker — Knowledge Bank

> Last Updated: February 20, 2026
> Version: 3.0

## Overview

Personal deal-discovery tool for finding motivated FSBO (For Sale By Owner) sellers in Charlotte NC and Nashville TN. Scans Redfin and Zillow daily, scores listings by motivation signals, and presents them in a Bloomberg-terminal-inspired dashboard. **Fully separated** from AVMLens as a standalone service (Feb 2026). Admin-only, auth-gated.

## Architecture

```
frontend/listing-tracker.html (standalone SPA, auth-gated)
    ↓ fetch()
fsbo_tracker/router.py (FastAPI router, 5 endpoints, registered in api/main.py)
    ↓ imports
fsbo_tracker/ (self-contained Python package at repo root)
    ├── config.py      — Markets, keyword tiers, scoring weights
    ├── db.py          — psycopg2 direct to Railway Postgres
    ├── redfin_fetcher.py — GIS-CSV bulk + detail page scrape
    ├── zillow_fetcher.py — async-create-search-page-state API
    ├── scorer.py      — Keyword regex + photo AI + price/DOM/cuts scoring
    ├── photo_analyzer.py — Claude Haiku 4.5 vision (on-demand)
    ├── tracker.py     — Daily pipeline orchestrator
    ├── run.py         — CLI entry point
    ├── __main__.py    — `python -m fsbo_tracker`
    └── migrations/038_fsbo_listings.sql
    ↓ writes to
Railway Postgres: fsbo_searches, fsbo_listings, fsbo_price_events
```

### Cross-Dependencies (Minimal)

| Direction | What | Where |
|-----------|------|-------|
| FSBO → AVMLens | `api.services.geo_service.enrich_property()` | `router.py` geo-enrich endpoint only |
| FSBO → AVMLens | Registered as router in `api/main.py` | `/api/v2/fsbo` prefix |
| AVMLens → FSBO | None | Zero reverse dependencies |
| Shared | `DATABASE_URL`, `ANTHROPIC_API_KEY`, proxy env vars | Environment only |

The listing_tracker module has **zero imports** from AVMLens core code. It could be extracted to a standalone service by removing the single geo enrichment call.

## File Inventory

| File | Lines | Purpose |
|------|-------|---------|
| `fsbo_tracker/config.py` | ~123 | Market bboxes, keyword patterns, score caps, thresholds |
| `fsbo_tracker/db.py` | ~457 | All DB operations: upsert, state transitions, queries |
| `fsbo_tracker/redfin_fetcher.py` | ~300 | GIS-CSV download + detail page scraping |
| `fsbo_tracker/zillow_fetcher.py` | ~250 | Zillow search API + detail enrichment |
| `fsbo_tracker/scorer.py` | ~170 | 5-axis scoring engine |
| `fsbo_tracker/photo_analyzer.py` | ~205 | Claude Haiku vision for property condition |
| `fsbo_tracker/tracker.py` | ~200 | Daily pipeline: fetch → parse → score → store |
| `fsbo_tracker/run.py` | ~80 | CLI with --migrate, --fetch, --score, --photos flags |
| `fsbo_tracker/router.py` | ~250 | 5 FastAPI endpoints (registered via api/main.py) |
| `fsbo_tracker/migrations/038_fsbo_listings.sql` | ~74 | Schema: 3 tables + indexes |
| `fsbo_tracker/geo_lite.py` | ~200 | 12-layer geo proximity (HIFLD + EPA + FEMA) |
| `frontend/listing-tracker.html` | ~3200 | Full SPA: cards, terminal, detail, map, settings |

## Database Schema

**3 tables** (migration `038_fsbo_listings.sql`), isolated from AVMLens platform DB:

### fsbo_searches
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | e.g. "charlotte-nc" |
| name | TEXT | Display name |
| region_id | INTEGER | Redfin region ID |
| min_lat, max_lat, min_lng, max_lng | REAL | Bounding box |
| max_price | INTEGER | Price cap filter |
| min_beds, min_dom | INTEGER | Fetch filters |
| grace_days | INTEGER | Days before "missing" → "gone" |
| active | BOOLEAN | Enable/disable market |

### fsbo_listings
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | `{source}_{property_id}` |
| search_id | TEXT FK | → fsbo_searches.id |
| source | TEXT | "redfin" or "zillow" |
| address, city, state, zip_code | TEXT | Full address components |
| latitude, longitude | REAL | For map + geo enrichment |
| listing_type | TEXT | "fsbo", "mls", etc. |
| price | INTEGER | Current ask price |
| beds, baths | REAL | Property details |
| sqft, year_built | INTEGER | Property details |
| property_type | TEXT | SFR, condo, etc. |
| dom | INTEGER | Days on market (from source) |
| days_seen | INTEGER | Days we've tracked it |
| status | TEXT | "active", "watched", "missing", "gone" |
| score | INTEGER | Composite 0-100 |
| score_breakdown | TEXT (JSON) | `{"keywords":N,"photos":N,"price_ratio":N,"dom":N,"cuts":N}` |
| keywords_matched | TEXT (JSON) | Array of matched keyword objects |
| remarks | TEXT | Listing description/comments |
| photo_urls | TEXT (JSON) | Array of photo URLs |
| photo_damage_score | INTEGER | 0-10 from Haiku vision |
| photo_damage_notes | TEXT | AI summary |
| photo_analysis_json | TEXT (JSON) | Full AI response |
| photo_analyzed_at | TIMESTAMP | When AI ran |
| assessed_value | INTEGER | Tax assessed value |
| redfin_estimate | INTEGER | Redfin AVM estimate |
| zestimate | INTEGER | Zillow Zestimate (sale AVM) |
| rent_zestimate | INTEGER | Zillow Rent Zestimate |
| last_sold_price | INTEGER | Most recent sale price |
| last_sold_date | TEXT | Most recent sale date |
| seller_name | TEXT | FSBO seller name |
| seller_phone | TEXT | Seller phone number |
| seller_email | TEXT | Seller email |
| seller_broker | TEXT | Listing broker if any |
| flood_zone | TEXT | FEMA flood zone from geo enrichment |
| flood_risk_level | TEXT | Flood risk level description |
| price_cuts | INTEGER | Count of price drops detected |
| last_price_cut_pct | REAL | Most recent cut % |
| last_price_cut_at | TIMESTAMP | When last cut detected |
| first_seen_at, last_seen_at | TIMESTAMP | Tracking window |
| gone_at | TIMESTAMP | When marked gone |
| grace_until | TIMESTAMP | Missing grace deadline |
| redfin_url, zillow_url | TEXT | Source listing URLs |
| detail_fetched_at | TIMESTAMP | When detail scrape ran |

### fsbo_price_events
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | Auto-increment |
| listing_id | TEXT FK | → fsbo_listings.id |
| price_before, price_after | INTEGER | Price change |
| change_pct | REAL | Percentage drop |
| detected_at | TIMESTAMP | When detected |

## Scoring System (100 points max)

| Axis | Max | How |
|------|-----|-----|
| Keywords | 40 | Regex scan of remarks. Tier A (7pts): motivated, as-is, estate, foreclosure, below market. Tier B (4pts): relocation, divorce, seller financing. Tier C (2pts): fixer, TLC, vacant, investor special. |
| Photos AI | 25 | Claude Haiku vision damage_score (0-10) × 2.5. Conditional trigger only. |
| Price/Value | 20 | Ask price / assessed_value ratio. <0.80 = 20pts, <0.90 = 12pts, <1.00 = 6pts. |
| DOM | 10 | ≥90d = 10pts, ≥55d = 6pts, ≥30d = 3pts. |
| Price Cuts | 5 | ≥2 cuts = 5pts, ≥1 cut = 3pts. |

**Thresholds**: High Priority ≥ 30, Shortlist ≥ 15 (config.py). Frontend uses 60/35 (adjustable in Settings).

### Photo AI Trigger Conditions
Only runs when other signals justify cost (~$0.002/listing):
- Keyword score ≥ 5, OR
- Price/assessed ratio ≤ 0.95, OR
- DOM ≥ 30 with ≥1 price cut, OR
- DOM ≥ 90 (stale regardless)

## API Endpoints

All under `/api/v2/fsbo/` prefix, registered in `api/main.py`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/fsbo/listings` | All active/watched listings + stats. Params: `search_id`, `min_score`, `include_gone` |
| GET | `/fsbo/listings/{id}` | Single listing + price history |
| POST | `/fsbo/listings/{id}/analyze-photos` | Trigger Haiku vision, store results, re-score |
| POST | `/fsbo/listings/{id}/geo-enrich` | Run 12-layer geo proximity via AVMLens geo_service |
| GET | `/fsbo/searches` | List configured market searches |

**All endpoints require `X-Admin-Password` header** (checked by `verify_fsbo_admin` dependency in router.py).

## Frontend (listing-tracker.html)

Single-page app, ~3200 lines, standalone HTML with Tailwind CDN + Leaflet.

### Pages
- **Search** — Filter bar + card/terminal toggle + Leaflet map
- **My Properties** — Saved favorites + archived
- **Settings** — Side-nav: General Defaults, Financing Defaults, Max Offer Calculator, Display & Data
- **Detail** — Full property drill-down (see below)

### Card View Features
- 180px photo with nav + photo count badge
- Two-column financial layout: flip metrics (left) | rental metrics (right)
- Inline pencil-edit on all financial fields (Purchase, ARV, Reno, Rent, Taxes, HOA)
- Condition grade buttons (A/B/C/D/F) inline with Reno — same as AVMLens
- Geo risk badges (deduped by layer type, worst adjustment per layer, max 5)
- Badges: HIGH, NEW, AI, price cuts, keyword pills
- Source link (RF/ZL), DOM, flood zone, seller contact indicator
- Offer status dropdown (14 options)
- Buttons: Remarks popup, Notes popup, AI analysis, Geo enrichment

### Detail Page Layout (dense trading terminal aesthetic)
**Left column (300px):**
1. Photo gallery (280px height, thumbstrip, nav arrows)
2. AI Photo Analysis (Haiku vision results, condition grade suggestion)
3. Google Maps embed (satellite view, Street View / Satellite / Directions links)
4. Property Info (price, assessed, AVMs, last sold, DOM, flood, $/sqft, source)
5. Seller Contact (name, phone, email, broker — shown if data exists)
6. Score Breakdown (5-axis bar chart + keyword pills)
7. Remarks (keyword-highlighted, scrollable)

**Right column (flex):**
1. Flip Analysis — inline label+input rows (Purchase, Reno w/ grade buttons, ARV) + computed metrics
2. Rental Analysis — inline rows (Rent/mo + AVM ref, Taxes/yr, HOA/mo) + computed metrics
3. Status + Notes
4. Geo Risk Factors — factor list + mini-map with polylines

**All sections are collapsible** (chevron toggle, display:none). State resets when opening a new property.

### Condition Grade System (ported from AVMLens)
| Grade | Label | Multiplier | Color |
|-------|-------|------------|-------|
| A | Minor | 2 | Green |
| B | Light | 4 | Light green |
| C | Moderate | 7 | Yellow |
| D | Heavy | 10 | Orange |
| F | Gut | 13 | Red |

Reno formula: `ageFactor × sqft × multiplier + boost`
- Age ≤40: `ageFactor = age / 20.3`
- Age >40: linear at 40 + `(sqrt(age) - sqrt(40)) × 0.31`
- D/F boost for newer homes: `boostAmount × (40 - age) / 40`
- Default grade: B (pre-1975: C)
- AI photo analysis suggests a grade which auto-applies

### Rent AVM Logic
- Scraped `rent_zestimate` only — no formula fallback
- Rounding: subtract $10, floor to nearest $10 (`Math.floor((rent_zestimate - 10) / 10) * 10`)
- If null, shows "—" (no estimate)

### Financial Calculations (client-side, from appSettings)
```
basis = purchasePrice + closingCosts + renoBudget + carryingCosts
closingCosts = purchasePrice × acqCostsPct
holdingPeriod = 60 + floor(renoBudget / 10000) × 7 days
carryingCosts = max(purchasePrice × 0.007, 500) / 30 × holdingPeriod
netProfit = ARV - basis - brokerageCosts - salesClosingCosts
ROI = netProfit / cashInvested × 100
maxOffer = ARV × flipFactor - renoBudget - minFlipProfit
```

### Inline Card Editing
All financial fields on cards have pencil icons. Click opens an input, Enter/blur commits:
- Saves to `propertyFinancials[listingId]` in localStorage
- Rebuilds card with formatted values (toLocaleString commas)
- Syncs bidirectionally with detail page if open
- Reno manual edit clears condition grade (manual = no grade)

### Persistence (localStorage)
- `fsboTrackerState` — favorites, notes, statuses, financials (inc. conditionGrade), geoCache, viewMode
- `fsboSettings` — all financial defaults, scoring thresholds, display prefs

### Data Loading
1. Try `API_BASE/fsbo/listings` (live Railway API)
2. Fallback to `/fsbo_latest.json` or `./fsbo_latest.json` (static export)

## Markets

| ID | Name | Redfin Region | Price Cap | Bbox |
|----|------|---------------|-----------|------|
| charlotte-nc | Charlotte NC MSA | 3105 | $500k | 34.88–35.58 N, 80.38–81.25 W |
| nashville-tn | Nashville TN MSA | 13415 | $600k | 35.83–36.46 N, 86.35–87.10 W |

## Data Sources

| Source | Method | What We Get |
|--------|--------|-------------|
| Redfin GIS-CSV | `curl_cffi` + proxy | Bulk listings (address, price, beds, baths, sqft, DOM, coords, URL) |
| Redfin Detail | `curl_cffi` + proxy | Remarks, photos, assessed value, Redfin estimate |
| Zillow Search | `async-create-search-page-state` | Listings + photos + price change data |
| Zillow Detail | Property page scrape | Remarks, additional photos |
| Claude Haiku 4.5 | Anthropic API (base64 images) | damage_score 0-10, work items, red flags, opportunity notes |

## CLI Usage

```bash
cd avm_platform
python -m fsbo_tracker --migrate-only            # Create/update tables
python -m fsbo_tracker                           # Full daily run
python -m fsbo_tracker --market charlotte-nc     # Single market
python -m fsbo_tracker --score-only              # Re-score all active listings
python -m fsbo_tracker --analyze-photos <id>     # On-demand photo AI
python -m fsbo_tracker --descriptions-only       # Fetch missing descriptions
```

## Environment Variables

| Var | Required | Used By |
|-----|----------|---------|
| `FSBO_DATABASE_URL` or `DATABASE_URL` | Yes | db.py (Railway Postgres). FSBO_DATABASE_URL preferred. |
| `ANTHROPIC_API_KEY` | For photo AI | photo_analyzer.py |
| `IPROYAL_USER`, `IPROYAL_PASS` | For Redfin | redfin_fetcher.py proxy |
| `OXYLABS_USER`, `OXYLABS_PASS` | For Zillow | zillow_fetcher.py proxy |
| `ADMIN_PASSWORD` | For API auth | router.py (X-Admin-Password header check) |
| `FSBO_ENABLED` | No (default: true) | api/main.py — feature flag to disable FSBO router |

## Deployment

**Fully separated** from AVMLens as of Feb 19, 2026.

| Component | Platform | URL |
|-----------|----------|-----|
| Backend API | Railway (`railway up` CLI) | `https://fsbo-api-production.up.railway.app` |
| Frontend | Netlify (`netlify deploy --prod --dir=frontend`) | `https://fsbo-tracker.netlify.app` |
| Source | GitHub | `jnewg4-coder/fsbo-tracker` |
| Local dev | `~/Projects/fsbo-tracker/` | — |

**Deploy workflow:** edit → commit → push → `railway up` (backend) + `netlify deploy --dir=frontend --prod` (frontend).

Railway env vars: `FSBO_DATABASE_URL`, `ADMIN_PASSWORD`, `ANTHROPIC_API_KEY`, `IPROYAL_*`

## Geo Risk Layers

12 layers via HIFLD + EPA + FEMA ArcGIS:

| Layer | Icon | Decay | Source |
|-------|------|-------|--------|
| Highway | 🛣️ | Exponential | HIFLD |
| Railroad | 🚂 | Exponential | HIFLD |
| Cul-de-sac | 🔵 | Binary +12% | HIFLD |
| Transmission | ⚡ | Exponential | HIFLD |
| Sewage | 🏭 | Exponential | HIFLD |
| Airport | ✈️ | Exponential | HIFLD |
| Cell Tower | 📡 | Exponential | HIFLD |
| Noise | 🔊 | Stepped | HIFLD |
| Superfund | ☢️ | Exponential | EPA FRS Layer 22 |
| Brownfield | 🏚️ | Exponential | EPA FRS Layer 0 |
| TRI | 🧪 | Exponential | EPA FRS Layer 23 |
| Flood | 🌊 | N/A (zone) | FEMA NFHL |

Adjustments are **informational only** — displayed on cards/detail but not factored into financial calculations.

## Known Limitations

1. **Zillow descriptions often truncated** — "flex text" gives limited keyword matches vs full remarks
2. **No scheduled runner** — CLI must be run manually or via cron; no Railway cron job configured yet
3. **Google Maps embed** — uses basic embed (no API key); Street View link opens in new tab rather than inline
