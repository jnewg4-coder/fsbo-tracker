# FSBO Listing Tracker — Knowledge Bank

> Last Updated: February 18, 2026
> Version: 1.0

## Overview

Personal deal-discovery tool for finding motivated FSBO (For Sale By Owner) sellers in Charlotte NC and Nashville TN. Scans Redfin and Zillow daily, scores listings by motivation signals, and presents them in a Bloomberg-terminal-inspired dashboard. Not customer-facing — admin-only tool within AVMLens platform.

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
| `frontend/listing-tracker.html` | ~2100 | Full SPA: cards, terminal, detail, map, settings |

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

Single-page app, ~2100 lines, standalone HTML with Tailwind CDN + Leaflet.

### Pages
- **Search** — Filter bar + card/terminal toggle + Leaflet map
- **My Properties** — Saved favorites + archived
- **Settings** — Side-nav: General Defaults, Financing Defaults, Max Offer Calculator, Display & Data
- **Detail** — Full property drill-down (photo gallery, flip analysis, rental analysis, AI photo review, geo risk)

### Card View Features
- 200px photo grid with nav arrows
- Swipeable data panels: Panel 1 (property data + score bars), Panel 2 (financial metrics matching Picket Pro reference)
- Mouse drag + touch swipe + arrow buttons + dot indicators
- Full address: `123 Main St, Charlotte, NC 28205`
- Badges: HIGH, WATCHING, NEW, AI, price cuts
- Keyword pills from matched terms
- Offer status dropdown (14 options)

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

### Persistence (localStorage)
- `fsboTrackerState` — favorites, notes, statuses, financials, geoCache, viewMode
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

## Known Limitations

1. **No HOA / property_taxes columns** in DB schema — card shows "—" for these fields
2. **Zillow descriptions often truncated** — "flex text" gives limited keyword matches vs full remarks
3. **Single geo coupling** — geo-enrich endpoint optionally imports from AVMLens core (501 fallback); all other FSBO code is isolated
4. **No scheduled runner** — CLI must be run manually or via cron; no Railway cron job configured yet
