# FSBO Listing Tracker — Knowledge Bank

> Last Updated: February 27, 2026
> Version: 4.4

## Overview

Real-time FSBO (For Sale By Owner) listing intelligence SaaS for real estate investors. Scans Redfin and Zillow daily across 28 CBSA metro areas, scores listings by motivation signals, and presents them in a Bloomberg-terminal-inspired dashboard. **Fully separated** from AVMLens as a standalone service. Multi-user auth (JWT + bcrypt + Google OAuth), tiered subscriptions via Helcim, PWA-enabled.

**New in v4.4:** Price-range split fetching ($20k-$499k + $500k-$800k) overcomes Redfin 350-per-request cap, foreclosures removed (FSBO + MLS-FSBO only), dynamic listing count on landing page (rounded to nearest 1,000), cross-promo copy hardened ("screening tool" positioning, legal disclaimer), config→DB search sync on pipeline start, `up to N markets` phrasing for trust safety.

**New in v4.3:** Custom domain (`fsbotracker.app`), Netlify API proxy (`/api/*` → Railway), JWT auth + Google OAuth + Helcim billing, Slack alerts, rate limiting (slowapi), global error handler + request ID middleware, toast notifications, TOS/Privacy pages, PWA (manifest, service worker, offline fallback, iOS install nudge), cross-promo tickers (AVMLens ↔ FSBO), 28 CBSA markets (up from 8), $800k max price, dynamic market counts.

**New in v4.2:** NDVI vegetation badge + filter, multi-select dropdowns (Status/Vegetation), AVM-derived purchase default (AVM − $700 rounded), Google Street View via StreetViewPanorama + computeHeading, auto-geo enrichment (viewport-scoped, 60-day cache), tax assessed value on cards, detail photo scroll (wheel + touch swipe), square card edges.

## Architecture

```
frontend/ (Netlify — fsbotracker.app)
    ├── index.html             — Landing page (SEO, dual cross-promo ticker)
    ├── listing-tracker.html   — Main SPA (auth-gated, PWA)
    ├── pricing.html           — Pricing tiers (Free/$29/$59/$99)
    ├── terms.html / privacy.html — Legal pages
    ├── offline.html           — PWA offline fallback
    ├── manifest.json / sw.js  — PWA service worker + manifest
    ├── _redirects             — Netlify routing + /api/* proxy → Railway
    └── icons/                 — SVG + PNG icons
    ↓ fetch('/api/v2/...')  → Netlify proxy → Railway
fsbo_tracker/app.py (FastAPI app — lifespan, middleware, error handling)
    ├── fsbo_tracker/auth_router.py  (login/signup/oauth under /api/v2/auth)
    ├── fsbo_tracker/billing_router.py (Helcim subscriptions under /api/v2/billing)
    ├── fsbo_tracker/router.py (5 listing endpoints under /api/v2/fsbo)
    ├── deal_pipeline/router.py (20 deal endpoints under /api/v2/deals)
    ├── fsbo_tracker/slack_alerts.py  (Slack webhook alerter, [FSBO] prefix)
    └── fsbo_tracker/rate_limit.py   (slowapi rate limiting + real-IP extraction)
    ↓ imports
fsbo_tracker/ (deal discovery package)
    ├── config.py      — Markets, keyword tiers, scoring weights
    ├── db.py          — psycopg2 direct to Railway Postgres
    ├── auth_service.py — JWT + bcrypt + brute-force lockout
    ├── auth_db.py     — User CRUD (fsbo_users table)
    ├── auth_router.py — Login/signup/OAuth/profile endpoints
    ├── billing_router.py — Helcim plan init + subscribe + webhook
    ├── access.py      — Tier-based access control (markets, AI actions, features)
    ├── redfin_fetcher.py — GIS-CSV bulk + detail page scrape
    ├── zillow_fetcher.py — async-create-search-page-state API
    ├── scorer.py      — Keyword regex + photo AI + price/DOM/cuts scoring
    ├── photo_analyzer.py — Claude Haiku 4.5 vision (on-demand)
    ├── tracker.py     — Daily pipeline orchestrator
    ├── ndvi_lite.py   — NAIP NDVI overgrowth enrichment
    ├── geo_lite.py    — 12-layer geo proximity (HIFLD + EPA + FEMA)
    ├── slack_alerts.py — Slack webhook alerts ([FSBO] prefix, 5-min cooldown)
    ├── rate_limit.py  — slowapi rate limiter + real-IP extraction
    ├── run.py         — CLI entry point
    ├── __main__.py    — `python -m fsbo_tracker`
    └── migrations/038_fsbo_listings.sql
deal_pipeline/ (deal execution package — Offer → Closing)
    ├── config.py             — BUY stage graph, transitions, requirements, tier limits
    ├── sell_stage_config.py  — SELL stage graph (placeholder)
    ├── db.py                 — Deal CRUD, stage advance, contacts, docs, inspections
    ├── router.py             — 20 FastAPI endpoints (/api/v2/deals)
    ├── offer_writer.py       — AI offer generation (placeholder → NotImplementedError)
    └── migrations/043_deal_pipeline.sql
    ↓ writes to
Railway Postgres: fsbo_searches, fsbo_listings, fsbo_price_events, fsbo_users,
                  deals, deal_contacts, deal_documents, deal_inspections,
                  deal_activity_log, offer_drafts
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
| `fsbo_tracker/ndvi_lite.py` | ~130 | NAIP NDVI overgrowth enrichment (ported from AVMLens) |
| `frontend/listing-tracker.html` | ~5925 | Full SPA: cards, terminal, detail, map, settings, pipeline (v2 design system) |
| `deal_pipeline/__init__.py` | ~5 | Package marker |
| `deal_pipeline/config.py` | ~105 | BUY stages, transitions, requirements, tier limits |
| `deal_pipeline/sell_stage_config.py` | ~35 | SELL stages placeholder |
| `deal_pipeline/db.py` | ~575 | Deal CRUD + stage advance + contacts/docs/inspections |
| `deal_pipeline/router.py` | ~405 | 20 endpoints under /api/v2/deals |
| `deal_pipeline/offer_writer.py` | ~15 | AI offer writer placeholder |
| `deal_pipeline/migrations/043_deal_pipeline.sql` | ~205 | 6 tables + indexes |

## Database Schema

**9 tables** across 2 migrations, isolated from AVMLens platform DB:

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

**Auth:** JWT Bearer token (from login/signup) or `X-Admin-Password` header (backward-compat admin fallback).

### Auth Endpoints (`/api/v2/auth/`)

| Method | Path | Purpose | Rate Limit |
|--------|------|---------|------------|
| POST | `/auth/signup` | Register new user (bcrypt password) | 5/min |
| POST | `/auth/login` | Login → JWT token (24hr) | 5/min |
| GET | `/auth/me` | Current user profile | — |
| GET | `/auth/google` | Google OAuth redirect | — |
| GET | `/auth/google/callback` | OAuth callback → JWT | — |

### Billing Endpoints (`/api/v2/billing/`)

| Method | Path | Purpose | Rate Limit |
|--------|------|---------|------------|
| GET | `/billing/plans` | List Helcim subscription plans | 20/min |
| POST | `/billing/initialize` | Init Helcim checkout.js session | 3/min |
| POST | `/billing/subscribe/{plan_id}` | Activate subscription | 3/min |
| POST | `/billing/webhook` | Helcim webhook handler | — |

## Frontend (listing-tracker.html)

Single-page app, ~4910 lines, standalone HTML with Tailwind CDN + Leaflet + JetBrains Mono.

### Design System v2 (Phase 1 — Bloomberg/Trading Terminal)

**Scope:** Pipeline + Deal Detail pages only. Search page completely untouched.

**Design tokens** (CSS custom properties on `:root`):
- Backgrounds: `--bg-base: #08080d` (near-black), `--bg-surface: #101018`, `--bg-elevated: #181822`
- Text: `--text-primary: #e4e4ec`, `--text-accent: #06b6d4` (cyan)
- Status: `--clr-green: #00d4aa`, `--clr-red: #ff4466`, `--clr-amber: #ffaa00`, `--clr-blue: #3388ff`, `--clr-purple: #aa66ff`
- Typography: `--font-data: 'JetBrains Mono'` (numbers/data), Inter (labels/UI)
- Spacing: tight (8px gaps, 4px padding in data cells), sharp radii (2-4px)

**Component classes:** `.fin-panel`, `.fin-panel-header`, `.panel-body`, `.pipe-table`, `.pipe-table-wrap`, `.pipe-tabs-v2`, `.pipe-tab-v2`, `.pipe-side-v2`, `.stage-pip`, `.status-dot`, `.money`, `.data-mono`, `.dd-header-v2`, `.dd-stage-progress`, `.dd-stage-node`, `.dd-grid-v2`, `.notes-panel`, `.activity-feed`, `.btn-v2`

### Pages
- **Search** — Filter bar + card/terminal toggle + Leaflet map (**unchanged by design overhaul**)
- **My Properties** — Saved favorites + archived
- **Settings** — Side-nav: General Defaults, Financing Defaults, Max Offer Calculator, Display & Data
- **Detail** — Full property drill-down (see below)

### Card View Features
- 180px photo with nav + photo count badge
- Square card edges (no border-radius)
- Two-column financial layout: flip metrics (left) | rental metrics (right)
- Inline pencil-edit on all financial fields (Purchase, ARV, Reno, Rent, Taxes, HOA)
- AVM labels on Purchase and ARV fields (e.g. "AVM: $200,000") — persists while user edits value
- Tax assessed value row below HOA (when available, gray de-emphasized)
- Condition grade buttons (A/B/C/D/F) inline with Reno — same as AVMLens
- Geo risk badges (deduped by layer type, worst adjustment per layer, max 5)
- NDVI vegetation badge (HIGH/MODERATE/LOW/MINIMAL with color coding)
- Badges: HIGH, NEW, AI, price cuts, keyword pills, vegetation
- Source link (RF/ZL), DOM, flood zone, seller contact indicator
- Offer status dropdown (14 options)
- Buttons: Remarks popup, Notes popup, AI analysis, Geo enrichment

### Detail Page Layout (dense trading terminal aesthetic)
**Left column (300px):**
1. Photo gallery (280px height, thumbstrip, nav arrows, mouse wheel + touch swipe scrolling)
2. AI Photo Analysis (Haiku vision results, condition grade suggestion)
3. Google Street View (StreetViewPanorama + computeHeading to face property) + Satellite toggle
4. Property Info (price, assessed, AVMs, last sold, DOM, flood, $/sqft, source)
5. Seller Contact (name, phone, email, broker — shown if data exists)
6. Score Breakdown (5-axis bar chart + keyword pills)
7. Remarks (keyword-highlighted, scrollable)

**Right column (flex):**
1. Flip Analysis — inline label+input rows (Purchase w/ AVM label, Reno w/ grade buttons, ARV w/ AVM label) + computed metrics
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

### Purchase AVM Logic
- Default: `bestAVM - $700`, round down to nearest $1,000 (`Math.floor((avm - 700) / 1000) * 1000`)
- bestAVM = `redfin_estimate || zestimate || 0`
- Clamp: if AVM < $700, result = 0 → fallback to `price × estPurchasePct%` (default 95%)
- User-edited purchase persists via `propertyFinancials[id].purchase` (survives page reload)
- Nullish guard: `fin.purchase != null` (not `||`) — preserves intentional $0

### ARV AVM Logic
- Default: `redfin_estimate || zestimate || assessed_value || price`
- User-edited ARV persists via `propertyFinancials[id].arv`
- Nullish guard: `fin.arv != null` (not `||`) — preserves intentional $0
- AVM label shown on detail page input (e.g. "AVM: $200,000")

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

### Google Street View
- Uses `StreetViewPanorama` (not iframe embed) for correct camera heading
- Dynamic script loading: `loadGmaps(key)` appends `libraries=geometry` script with inflight promise dedupe
- `initStreetView(containerId, lat, lng)`: calls `StreetViewService.getPanorama()` with `radius:80, source:OUTDOOR`
- Camera faces property via `google.maps.geometry.spherical.computeHeading(cameraPosition, propertyCoords)`
- Lazy-load: only fetches Maps API key + renders when detail page opens (not on page load)
- Satellite view: still uses basic iframe embed (no API key needed)
- Maps key endpoint: `GET /api/v2/fsbo/maps-key` (cached in `_fsboMapsKeyPromise`)

### Filter Bar — Multi-Select Dropdowns
- **Market**, **Status**, **Vegetation** all use `market-dropdown` CSS pattern with checkboxes
- Pattern: `market-dropdown-btn` toggle → `market-dropdown-menu` with `market-dropdown-item` rows containing `<input type="checkbox">`
- Multi-select: selected values collected into array, "All" shown when nothing selected
- Status options: Active, Under Contract, Sold, Watched, Gone
- Vegetation/NDVI options: High, Moderate, Low, Minimal
- Close-on-outside-click registered for all 3 dropdowns

### NDVI Vegetation Enrichment
- Module: `fsbo_tracker/ndvi_lite.py` — NAIP NDVI via USGS ArcGIS REST
- Overgrowth thresholds: HIGH ≥0.7, MODERATE 0.55–0.7, LOW 0.35–0.55, MINIMAL <0.35
- Confidence: high (≤2yr), moderate (≤3yr), low (>3yr), none (missing)
- Column: `ndvi_level` on fsbo_listings (migration 045)
- Filter: Vegetation multi-select dropdown (frontend)
- Badge: colored pill on cards (red HIGH, yellow MODERATE, blue LOW, gray MINIMAL)
- Backfill: runs during pipeline Step 5c
- Refresh: 90-day cadence or missing-only
- Feature flag: `NDVI_ENRICHMENT_ENABLED` (default true)

### Auto-Geo Enrichment
- Runs on page load after data loads — enriches up to 10 listings per session
- Scoped to current map viewport (uses `maps['search-map'].getBounds().contains()`)
- 60-day cache TTL via `isGeoCacheStale()` — checks `geoCache[id]._cached_at` timestamp
- 1-second delay between API calls (background, non-blocking)
- Re-renders cards and geo layer bar after completion
- "Fetch N" button also scoped to map viewport (not all listings)

### Persistence (localStorage)
- `fsboTrackerState` — favorites, notes, statuses, financials (inc. conditionGrade), geoCache, viewMode
- `fsboSettings` — all financial defaults, scoring thresholds, display prefs
- `fsbo_panel_${dealId}` — per-deal panel collapse states (label → boolean)

### Data Loading
1. Try `API_BASE/fsbo/listings` (live Railway API)
2. Fallback to `/fsbo_latest.json` or `./fsbo_latest.json` (static export)

## Markets (28 CBSA Metro Areas)

All markets use CBSA-level bounding boxes and $800k max price. Count is dynamic via `TOTAL_MARKETS = len(SEARCHES)` in config.py.

| ID | CBSA Name | Redfin Region | Price Cap |
|----|-----------|---------------|-----------|
| charlotte-nc | Charlotte-Concord-Gastonia NC-SC | 3105 | $800k |
| nashville-tn | Nashville-Davidson-Murfreesboro-Franklin TN | 13415 | $800k |
| tampa-fl | Tampa-St. Petersburg-Clearwater FL | 16163 | $800k |
| greensboro-nc | Greensboro-High Point NC | 5988 | $800k |
| winston-salem-nc | Winston-Salem NC | 19175 | $800k |
| birmingham-al | Birmingham-Hoover AL | 1128 | $800k |
| little-rock-ar | Little Rock-North Little Rock-Conway AR | 9326 | $800k |
| akron-oh | Akron OH | 145 | $800k |
| atlanta-ga | Atlanta-Sandy Springs-Alpharetta GA | 623 | $800k |
| jacksonville-fl | Jacksonville FL | 7995 | $800k |
| memphis-tn | Memphis TN-MS-AR | 10344 | $800k |
| indianapolis-in | Indianapolis-Carmel-Anderson IN | 7914 | $800k |
| columbus-oh | Columbus OH | 3489 | $800k |
| san-antonio-tx | San Antonio-New Braunfels TX | 14783 | $800k |
| cleveland-oh | Cleveland-Elyria OH | 4145 | $800k |
| raleigh-nc | Raleigh-Cary NC | 35711 | $800k |
| orlando-fl | Orlando-Kissimmee-Sanford FL | 13655 | $800k |
| houston-tx | Houston-The Woodlands-Sugar Land TX | 8903 | $800k |
| st-louis-mo | St. Louis MO-IL | 16661 | $800k |
| kansas-city-mo | Kansas City MO-KS | 35751 | $800k |
| pittsburgh-pa | Pittsburgh PA | 15702 | $800k |
| knoxville-tn | Knoxville TN | 10200 | $800k |
| columbia-sc | Columbia SC | 4149 | $800k |
| chattanooga-tn | Chattanooga TN-GA | 3641 | $800k |
| detroit-mi | Detroit-Warren-Dearborn MI | 5665 | $800k |
| dallas-tx | Dallas-Fort Worth-Arlington TX | 30794 | $800k |
| lexington-ky | Lexington-Fayette KY | 11746 | $800k |
| philadelphia-pa | Philadelphia-Camden-Wilmington PA-NJ-DE-MD | 15502 | $800k |

## Data Sources

| Source | Method | What We Get |
|--------|--------|-------------|
| Redfin GIS-CSV | `curl_cffi` + proxy, price-range split ($20k-$499k + $500k-$800k) | Bulk listings (address, price, beds, baths, sqft, DOM, coords, URL). FSBO + MLS-FSBO only, no foreclosures. 350/request cap bypassed via 2 price ranges → effective 700/market. |
| Redfin Detail | `curl_cffi` + proxy | Remarks, photos (URL strings, not blobs), assessed value, Redfin estimate |
| Zillow Search | `async-create-search-page-state` | Listings + photos + price change data |
| Zillow Detail | Property page scrape | Remarks, additional photos |
| Claude Haiku 4.5 | Anthropic API (base64 images) | damage_score 0-10, work items, red flags, opportunity notes |

### Listing Types

| Type | DB Value | Description |
|------|----------|-------------|
| Pure FSBO | `fsbo` | Owner selling directly, NOT on MLS |
| MLS-FSBO | `mlsfsbo` | Owner selling via flat-fee MLS service ($200-$500 flat fee, no listing agent commission) |

Both types shown in UI with type filter dropdown. Foreclosures removed from fetcher as of v4.4.

### Storage

No blob/file storage. Photos stored as JSON arrays of CDN URLs (Redfin/Zillow) in Postgres `photo_urls` column. Photo AI analysis fetches images on-demand from source CDNs. Total DB footprint is negligible (~11k rows).

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
| `ADMIN_PASSWORD` | Yes | Backward-compat admin auth fallback |
| `JWT_SECRET` | Prod only | auth_service.py (HS256 signing). Falls back to ADMIN_PASSWORD in dev. |
| `ANTHROPIC_API_KEY` | For photo AI | photo_analyzer.py |
| `IPROYAL_USER`, `IPROYAL_PASS` | For Redfin | redfin_fetcher.py proxy |
| `OXYLABS_USER`, `OXYLABS_PASS` | For Zillow | zillow_fetcher.py proxy |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | For Google OAuth | auth_router.py (BACKLOG: GCP project needed) |
| `HELCIM_API_TOKEN` | For billing | billing_router.py (Helcim API) |
| `SLACK_WEBHOOK_URL` | For alerts | slack_alerts.py |
| `SLACK_ALERTS_ENABLED` | No (default: false) | Set true on Railway for production alerts |
| `RATE_LIMIT_ENABLED` | No (default: true) | rate_limit.py kill-switch |
| `ENVIRONMENT` | No (default: production) | app.py — hides stack traces when != development |

## Deployment

**Fully separated** from AVMLens as of Feb 19, 2026. Custom domain live Feb 26, 2026.

| Component | Platform | URL |
|-----------|----------|-----|
| Frontend | Netlify | `https://fsbotracker.app` (custom domain via IONOS DNS) |
| Backend API | Railway (CLI deploy) | `https://fsbo-api-production.up.railway.app` (proxied via Netlify) |
| Source | GitHub | `jnewg4-coder/fsbo-tracker` |
| Local dev | `~/Projects/fsbo-tracker/` | — |
| Domain | IONOS | `fsbotracker.app` (A→75.2.60.5, CNAME www→fsbo-tracker.netlify.app) |

**API proxy:** Frontend calls `/api/v2/*` (relative). Netlify `_redirects` proxies to Railway:
```
/api/* https://fsbo-api-production.up.railway.app/api/:splat 200!
```
No CORS needed — same domain from the browser's perspective.

**Deploy workflow:** edit → commit → push → `railway up` (backend) + `netlify deploy --dir=frontend --prod` (frontend).

**SSL:** Auto-provisioned Let's Encrypt via Netlify (free).

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

## Auth & Billing

### Authentication
- **JWT:** HS256, 24hr expiry, signed with `JWT_SECRET` (falls back to `ADMIN_PASSWORD`)
- **Password:** bcrypt hashed, stored in `fsbo_users` table
- **Brute-force:** 5 failed attempts → 15min lockout (per email)
- **Backward-compat:** `X-Admin-Password` header still works for admin access
- **Google OAuth:** Code deployed, hidden until `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` set
  - Redirect URI: `https://fsbo-api-production.up.railway.app/api/v2/auth/google/callback`
- **Roles:** admin, user, viewer (in JWT payload)
- **Frontend:** Login/Signup tabs on listing-tracker.html, localStorage token (`fsbo_token`)

### Billing (Helcim)
- **Tiers:** Free ($0), Starter ($29/mo), Growth ($59/mo), Pro ($99/mo)
- **Provider:** Helcim (card tokenization + recurring subscriptions, NOT Stripe)
- **Flow:** Frontend → `/billing/initialize` → Helcim checkout.js → `/billing/subscribe/{plan_id}`
- **Access control** (`access.py`): markets, AI actions/day, CSV export, deal pipeline gated by tier

### Tier Limits

| Feature | Free | Starter | Growth | Pro |
|---------|------|---------|--------|-----|
| Markets | 1 (redacted) | 1 (full) | 3 | All 28 |
| AI actions/day | 0 | 5 | 20 | 100 |
| Deal pipeline | No | Yes | Yes | Yes |
| CSV export | No | No | Yes | Yes |
| Full addresses | No | Yes | Yes | Yes |
| Photos + contacts | No | Yes | Yes | Yes |

## Production Hardening (Phase 3a)

### Slack Alerts
- Module: `fsbo_tracker/slack_alerts.py`
- All messages prefixed with `[FSBO]` (distinguishes from AVMLens alerts in shared channel)
- Rate-limited per alert type (5-min cooldown)
- Alert types: `alert_error`, `alert_billing_failure`, `alert_scraper_failure`, `alert_high_error_rate`, `alert_database_error`, `notify_signup`, `notify_subscription`
- Env: `SLACK_WEBHOOK_URL` + `SLACK_ALERTS_ENABLED=true` on Railway

### Rate Limiting
- Module: `fsbo_tracker/rate_limit.py` (slowapi)
- Real-IP extraction: handles Railway CGNAT + Cloudflare proxies
- Kill-switch: `RATE_LIMIT_ENABLED` env var (default true)
- Limits: login/signup 5/min, billing 3/min, pipeline 2/min, listings 30/min, deals 10/min

### Request Logging Middleware
- 8-char request ID per request (`request.state.request_id`)
- `X-Request-ID` + `X-Process-Time` response headers
- WARNING log for status >= 400

### Global Exception Handler
- Catches unhandled exceptions → structured JSON with `request_id` + `error_code`
- Hides stack traces in production (`ENVIRONMENT != development`)
- Fires Slack alert on 500s

### Toast Notifications (Frontend)
- `showToast(message, type, duration)` — fixed bottom-right, auto-dismiss, stackable
- Replaces `alert()` calls for API errors, billing errors, auth errors

## PWA

- **Manifest:** `/manifest.json` — standalone display, blue theme, 192+512 icons
- **Service Worker:** `/sw.js` (cache `fsbo-v2`)
  - Network-first: HTML pages (deploys take effect instantly)
  - Cache-first: CDN assets (Leaflet, fonts, Tailwind) — supports opaque responses
  - Network-only: API calls (`/api/*`, Railway URL, localhost)
  - Stale-while-revalidate: everything else
  - Offline fallback: `/offline.html`
- **iOS Install Nudge:** Custom banner on 3rd visit, reminder on 10th (localStorage counter)
- **Registered on all pages:** index, listing-tracker, pricing, terms, privacy

## Deal Pipeline Module

### Overview

Tracks properties from discovery through closing. **Dual-sided** from day 1: one `deals` table handles both BUY and SELL, differentiated by `side` + `stage_profile`. Shared core advance/terminate/activity logic; only the stage graph differs per side.

### Pipeline Stages

**BUY (stage_profile: buy_v1):**
`Offer → Contract → Title → Due Diligence → Retrade → Clear to Close → Closed`
(+ `Terminated` as dead-end from any stage. Retrade is skippable: DD → CTC directly if dd_status=clear.)

**SELL (stage_profile: sell_v1):** *(placeholder — stages defined, no logic yet)*
`Prep → List → Market → Showings → Offer Review → Contract → Close`

### Database Tables (migration 043)

| Table | Purpose |
|-------|---------|
| `deals` | UUID PK, side/stage_profile, property basics, all stage-specific fields (offer→closed), meta |
| `deal_contacts` | UUID PK, deal_id FK CASCADE, role (7 types), name/phone/email/company |
| `deal_documents` | UUID PK, deal_id FK CASCADE, BYTEA file_data, doc_type, ai_analysis_json |
| `deal_inspections` | UUID PK, deal_id FK CASCADE, inspection_type, status, findings_json |
| `deal_activity_log` | BIGSERIAL PK, append-only audit trail (action/detail/old_value/new_value) |
| `offer_drafts` | UUID PK, deal_id FK CASCADE, draft_type, AI model/version tracking, approval chain |

**Key constraints:**
- `idx_deals_listing_unique_active` — UNIQUE partial index on `(listing_id) WHERE listing_id IS NOT NULL AND archived = FALSE` (prevents duplicate promote race)
- All child tables CASCADE on deal delete
- Documents: 10MB per file, 50MB per deal, MIME type allowlist enforced

### API Endpoints (20 routes, all require X-Admin-Password)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/deals` | List deals (filter: stage, side, archived) |
| POST | `/deals` | Create manual deal (side auto-maps to stage_profile) |
| POST | `/deals/from-listing/{id}` | Promote FSBO listing → BUY deal (auto-fill) |
| GET | `/deals/stats` | Pipeline summary counts by side + stage |
| GET | `/deals/{id}` | Full deal + contacts + docs + inspections + activity + drafts |
| PATCH | `/deals/{id}` | Partial field update (whitelisted fields only) |
| DELETE | `/deals/{id}` | Soft archive |
| POST | `/deals/{id}/advance` | Stage transition (validates requirements + status gates) |
| POST | `/deals/{id}/terminate` | Kill deal from any active stage |
| POST | `/deals/{id}/contacts` | Add contact |
| PATCH | `/deals/{id}/contacts/{cid}` | Update contact (deal_id ownership enforced) |
| DELETE | `/deals/{id}/contacts/{cid}` | Remove contact (deal_id ownership enforced) |
| POST | `/deals/{id}/documents` | Upload file (chunked, sanitized filename) |
| GET | `/deals/{id}/documents/{did}/download` | Download file (deal_id ownership enforced) |
| DELETE | `/deals/{id}/documents/{did}` | Delete document (deal_id ownership enforced) |
| POST | `/deals/{id}/inspections` | Add inspection |
| PATCH | `/deals/{id}/inspections/{iid}` | Update inspection (deal_id ownership enforced) |
| POST | `/deals/{id}/analyze-inspection/{did}` | **501** (AI Phase 3) |
| POST | `/deals/{id}/offer-draft` | Create shell draft record |
| POST | `/deals/{id}/offer-draft/{did}/generate` | **501** (AI Phase 3) |

### Stage Transition Rules (buy_v1)

| From | To | Hard Requirements | Status Gates |
|------|----|-------------------|--------------|
| offer | contract | acceptance_date | — |
| contract | title | contract_signed_date, binding_date | — |
| title | due_diligence | title_ordered_date | — |
| due_diligence | retrade | — | dd_status in (issue, retrade_needed) |
| due_diligence | clear_to_close | — | dd_status = clear |
| retrade | clear_to_close | — | retrade_status in (accepted, countered) |
| clear_to_close | closed | closing_date, final_purchase_price | — |
| Any | terminated | — | Always allowed (except from closed/terminated) |

### Security Hardening (completed)

- **Auth:** timing-safe `hmac.compare_digest` on X-Admin-Password
- **XSS:** `escapeHTML()` escapes `& < > " '` (single-quote added); all dynamic values in onclick handlers wrapped in `esc()`
- **Stage bypass:** Client cannot supply stage on creation — always forced to first stage of profile
- **Tier bypass:** `tier` not in INSERT fields — derived server-side
- **MIME bypass:** Null/missing MIME type rejected (not skipped)
- **Ownership:** All child record operations enforce `AND deal_id = %s` in WHERE clause
- **Filename:** Sanitized on upload — `os.path.basename()` + regex strip + null byte removal + 255 char limit
- **Race condition:** `FOR UPDATE` row lock + unique partial index on listing_id + 409 on duplicate promote
- **Atomic merge:** `workflow_state` update uses Postgres `COALESCE(col::jsonb, '{}') || new::jsonb` in single UPDATE (no read-then-write race)
- **Debounce closure:** Field save captures `dealId` at call time, not at timeout execution

### Frontend — Pipeline Tab (v2 Design)

Added between "My Properties" and "Settings" in the nav bar.

**Pipeline List Page (dense data table):**
- BUY/SELL segmented toggle (`.pipe-side-v2`: BUY=cyan, SELL=purple)
- Stage tabs with counts, underline active indicator (`.pipe-tab-v2`)
- Dense `<table>` layout (`.pipe-table` in `.pipe-table-wrap` for mobile scroll):
  - Columns: Stage (colored pip + 3-letter abbreviation), Address, List price, Offer price, Days, Deadline
  - Prices right-aligned monospace, green/neutral color coding
  - Deadline: red ▲ if ≤2 days, amber if ≤5 days
  - Row hover highlight (`--bg-elevated`)
- "New Deal" modal (address, city/state/zip, side, offer price, notes)

**Deal Detail Page (multi-panel Bloomberg layout):**
- Sticky header (`.dd-header-v2`): back button + address + stage badge + side label
- Stage progression bar (`.dd-stage-progress`): dot nodes with connectors (past=green, current=cyan, future=muted). Mobile-scrollable.
- Multi-panel grid (`.dd-grid-v2`): 2-column desktop, 1-column mobile
  - **Workflow panel**: stage-specific fields, status dropdowns, deadlines
  - **Financials panel**: list/offer/EMD/costs with computed net proceeds, advance stage button
  - **Notes panel** (always visible): free-form textarea (auto-save 800ms debounce) + quick-add timestamped notes + activity log feed
  - **Contacts panel**: CRUD with 7 role types
  - **Documents panel**: upload/download/delete with auth headers
  - **Signing panel**: placeholder for Phase 2 (PandaDoc/DocuSign integration)
- All panels collapsible (`.fin-panel-header` + `.panel-body`), state persisted per deal in localStorage (`fsbo_panel_${dealId}`)
- All fields inline-editable with debounced auto-save (400ms fields, 800ms notes)
- "AI Offer Writer (Beta)" placeholder button (disabled)

**"Deal →" button** on listing detail sticky header — promotes to BUY deal with auto-fill.

### Phased Build Status

| Phase | Status | Scope |
|-------|--------|-------|
| **Phase 1: Skeleton** | **Complete** | Backend CRUD + stage transitions + frontend pipeline tab + deal detail + promote from listing |
| **Phase 1b: Design Overhaul** | **Complete** | Bloomberg/trading desk aesthetic — dense data table pipeline, multi-panel deal detail, collapsible sections, JetBrains Mono, near-black palette. Search page untouched. |
| Phase 2: Docs + Contacts + Inspections | **Complete** | Upload/download/delete endpoints, contact CRUD, inspection CRUD — all in Phase 1 build |
| **Phase 2b: Auth + Billing** | **Complete** | JWT auth (HS256 24hr) + bcrypt + Google OAuth (code deployed, GCP project needed) + Helcim billing (4 tiers: Free/$29/$59/$99) |
| **Phase 3a: Hardening** | **Complete** | Slack alerts ([FSBO] prefix), rate limiting (slowapi), global error handler + request ID middleware, toast notifications, TOS/Privacy pages |
| **Phase 3b: PWA + Domain** | **Complete** | PWA (manifest, service worker, offline fallback, iOS install nudge), custom domain (fsbotracker.app), Netlify API proxy, cross-promo tickers (AVMLens ↔ FSBO), 28 CBSA markets, dynamic market counts |
| Phase 3c: SELL Pipeline + Signing Services | Not started | Google Sheet TC workflow (Pre-List → Sold), PandaDoc/DocuSign integration, list price worksheet |
| Phase 4: AI Integration | Placeholder | Inspection PDF analysis, offer writer, findings display, retrade auto-populate |
| Phase 5: Teams & Permissions | Not started | User roles (TC, Client, Admin), team management, permission-based field visibility |

## Known Limitations

1. **Zillow descriptions often truncated** — "flex text" gives limited keyword matches vs full remarks
2. **No scheduled runner** — CLI must be run manually or via cron; no Railway cron job configured yet
3. **Google Maps API key required** — Street View uses StreetViewPanorama (requires API key fetched from backend); satellite still uses free iframe embed
4. **SELL pipeline is placeholder only** — stages defined but no field definitions, validation, or frontend rendering yet. Next up: Phase 3c
5. **Tier limits partially enforced** — `access.py` defines limits per tier, wired into some endpoints; full enforcement pending
6. **Migrations re-run on every startup** — safe today (all `IF NOT EXISTS`) but needs tracking table before any `ALTER TABLE` migrations
7. **No Pydantic request models** — deal endpoints accept raw `dict` bodies; input validation relies on DB constraints
8. **No tests for deal_pipeline** — backend needs unit tests for stage transitions and CRUD
9. **Google OAuth not active** — code deployed but needs GCP project + env vars (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
10. **No transactional email** — verification, password reset, billing receipts not yet implemented (Brevo planned)

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 4.4 | 2026-02-27 | Price-range split fetching (overcomes 350 cap), foreclosures removed (FSBO + MLS-FSBO only), dynamic listing count on landing page, cross-promo copy hardened (screening tool positioning, legal disclaimer), config→DB search sync, `up to N` phrasing |
| 4.3 | 2026-02-27 | Custom domain (fsbotracker.app), Netlify API proxy, JWT auth + Google OAuth + Helcim billing (4 tiers), Slack alerts + rate limiting + global error handler, PWA (manifest, SW, offline, iOS nudge), TOS/Privacy pages, cross-promo tickers, 28 CBSA markets ($800k cap), dynamic market counts |
| 4.2 | 2026-02-24 | NDVI vegetation badge + filter, multi-select dropdowns (Status/Vegetation), AVM-derived purchase default (AVM−$700 rounded), Street View via StreetViewPanorama + computeHeading, auto-geo enrichment (viewport-scoped, 60-day cache), tax assessed on cards, detail photo scroll, square card edges, audit fixes (purchase clamp, nullish coalescing, gmaps dedupe, geo viewport gating) |
| 4.1 | 2026-02-21 | Phase 1 Design System Overhaul — Bloomberg aesthetic for Pipeline + Deal Detail, JetBrains Mono, near-black palette, P1/P2 audit fixes |
| 4.0 | 2026-02-20 | Deal Pipeline module — BUY/SELL stages, CRUD, stage transitions, promote from listing |
| 3.0 | 2026-02-19 | FSBO standalone separation from AVMLens, Railway + Netlify deploy |
