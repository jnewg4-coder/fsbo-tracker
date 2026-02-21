# FSBO Listing Tracker — Knowledge Bank

> Last Updated: February 21, 2026
> Version: 4.1

## Overview

Personal deal-discovery tool for finding motivated FSBO (For Sale By Owner) sellers in Charlotte NC and Nashville TN. Scans Redfin and Zillow daily, scores listings by motivation signals, and presents them in a Bloomberg-terminal-inspired dashboard. **Fully separated** from AVMLens as a standalone service (Feb 2026). Admin-only, auth-gated.

**New in v4.0:** Deal Pipeline module — track properties from Offer through Closing with full transaction coordination, dual-sided (BUY/SELL), stage transitions, document uploads, contacts, inspections, and AI placeholders.

**New in v4.1:** Phase 1 Design System Overhaul — Bloomberg/trading desk aesthetic for Pipeline + Deal Detail pages. Dense data table, multi-panel deal view, collapsible sections with per-deal persistence, JetBrains Mono data font, near-black terminal palette. P1/P2 audit fixes: atomic JSONB workflow merge, SELL stats normalization, promote duplicate → 409, mobile scroll containers.

## Architecture

```
frontend/listing-tracker.html (standalone SPA, auth-gated)
    ↓ fetch()
fsbo_tracker/app.py (FastAPI app — lifespan runs migrations on boot)
    ├── fsbo_tracker/router.py (5 listing endpoints under /api/v2/fsbo)
    └── deal_pipeline/router.py (20 deal endpoints under /api/v2/deals)
    ↓ imports
fsbo_tracker/ (deal discovery package)
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
deal_pipeline/ (deal execution package — Offer → Closing)
    ├── config.py             — BUY stage graph, transitions, requirements, tier limits
    ├── sell_stage_config.py  — SELL stage graph (placeholder)
    ├── db.py                 — Deal CRUD, stage advance, contacts, docs, inspections
    ├── router.py             — 20 FastAPI endpoints (/api/v2/deals)
    ├── offer_writer.py       — AI offer generation (placeholder → NotImplementedError)
    └── migrations/043_deal_pipeline.sql
    ↓ writes to
Railway Postgres: fsbo_searches, fsbo_listings, fsbo_price_events,
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
| `frontend/listing-tracker.html` | ~4910 | Full SPA: cards, terminal, detail, map, settings, pipeline (v2 design system) |
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

**All endpoints require `X-Admin-Password` header** (checked by `verify_fsbo_admin` dependency in router.py).

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
- `fsbo_panel_${dealId}` — per-deal panel collapse states (label → boolean)

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
| Phase 3: SELL Pipeline + Signing Services | Not started | Google Sheet TC workflow (Pre-List → Sold), PandaDoc/DocuSign integration, list price worksheet |
| Phase 4: AI Integration | Placeholder | Inspection PDF analysis, offer writer, findings display, retrade auto-populate |
| Phase 5: Teams & Permissions | Not started | User roles (TC, Client, Admin), team management, permission-based field visibility |
| Phase 6: Billing | Not started | Tier enforcement, free limits, upgrade prompts |

## Known Limitations

1. **Zillow descriptions often truncated** — "flex text" gives limited keyword matches vs full remarks
2. **No scheduled runner** — CLI must be run manually or via cron; no Railway cron job configured yet
3. **Google Maps embed** — uses basic embed (no API key); Street View link opens in new tab rather than inline
4. **SELL pipeline is placeholder only** — stages defined but no field definitions, validation, or frontend rendering yet. Next up: Phase 3 (Google Sheet TC workflow)
5. **Tier limits defined but not enforced** — admin-only for now; `check_tier_limit()` exists but not called from endpoints
6. **Migrations re-run on every startup** — safe today (all `IF NOT EXISTS`) but needs tracking table before any `ALTER TABLE` migrations
7. **No Pydantic request models** — deal endpoints accept raw `dict` bodies; input validation relies on DB constraints
8. **No tests for deal_pipeline** — backend needs unit tests for stage transitions and CRUD
