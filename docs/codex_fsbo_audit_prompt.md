# Codex Audit Prompt — FSBO Listing Tracker

## Context

You are auditing a **standalone FSBO (For Sale By Owner) listing tracker** embedded within a larger real estate platform (AVMLens). The tracker is a personal deal-discovery tool — not customer-facing. It scrapes Redfin and Zillow for FSBO listings in Charlotte NC and Nashville TN, scores them by motivation signals, and presents them in a Bloomberg-terminal-inspired dashboard.

**Read `docs/fsbo_knowledge_bank.md` first** — it contains the full architecture, scoring system, DB schema, API endpoints, and known limitations.

## Files to Audit (in priority order)

### Backend (Python) — all under `fsbo_tracker/`
1. `fsbo_tracker/db.py` — Database layer (psycopg2 direct, Railway Postgres)
2. `fsbo_tracker/redfin_fetcher.py` — Redfin GIS-CSV bulk fetch + detail scrape
3. `fsbo_tracker/zillow_fetcher.py` — Zillow search API + detail enrichment
4. `fsbo_tracker/tracker.py` — Daily pipeline orchestrator
5. `fsbo_tracker/scorer.py` — 5-axis scoring engine
6. `fsbo_tracker/photo_analyzer.py` — Claude Haiku vision analysis
7. `fsbo_tracker/config.py` — Markets, keywords, thresholds
8. `fsbo_tracker/run.py` — CLI entry point
9. `fsbo_tracker/router.py` — FastAPI router (5 endpoints, admin-auth gated)
10. `fsbo_tracker/migrations/038_fsbo_listings.sql` — Schema migration

### Frontend (HTML/JS)
11. `frontend/listing-tracker.html` — Full SPA (~2100 lines: cards, terminal, detail page, map, settings, financial calculators)

### Context files (read-only, for understanding cross-dependencies)
- `experiments/scrapers_v2/api/main.py` — Where fsbo router is registered (import from `fsbo_tracker.router`)
- `experiments/scrapers_v2/api/services/geo_service.py` — The ONE cross-dependency (used by router.py geo-enrich endpoint)

## Audit Severity Levels

- **P0 — Critical**: Will crash in production, corrupt data, or leak secrets. Must fix before deploy.
- **P1 — High**: Incorrect behavior, data loss risk, security holes, resource leaks. Fix soon.
- **P2 — Medium**: Logic errors, missing error handling, poor UX, performance issues. Fix when convenient.
- **P3 — Low**: Code style, minor improvements, nice-to-haves. Optional.

## What to Check

### P0 Candidates
- **SQL injection**: Any string interpolation in SQL queries (should use parameterized queries everywhere)
- **Unhandled DB connections**: Every `get_conn()` or `db_cursor()` must be properly closed (check try/finally or context manager usage)
- **Postgres transaction poisoning**: After a failed statement, the transaction is poisoned. Every `db_cursor()` must rollback on error (check the context manager in db.py)
- **Division by zero**: Any price/value ratio, percentage, or per-sqft calculation without denominator guards
- **Secret leaks**: API keys, DB URLs, or error details exposed in HTTP responses or console logs

### P1 Candidates
- **Race conditions in upsert**: Two concurrent runs could double-count price cuts or create duplicate listings
- **Missing COALESCE / NULL handling**: Postgres NULLs in arithmetic silently produce NULL
- **Photo analyzer error propagation**: If Haiku returns malformed JSON, does it crash or degrade gracefully?
- **Frontend XSS**: Any `innerHTML` assignment with unescaped user-controlled data (listing remarks, addresses, notes)
- **localStorage corruption**: If JSON.parse fails on corrupted localStorage, does the app crash or recover?
- **Auth bypass**: Verify that all FSBO endpoints require `X-Admin-Password` header via `verify_fsbo_admin`

### P2 Candidates
- **Error handling gaps**: Catch-all `except Exception` that swallows useful errors
- **Proxy fallback logic**: Does Redfin/Zillow fetcher handle proxy failures gracefully? Retry logic?
- **Score consistency**: If scorer.py uses different thresholds than config.py, or if frontend thresholds diverge from backend
- **Stale data**: Listings marked "gone" staying in DB forever (no cleanup/archival)
- **Photo cost control**: Is MAX_PHOTOS (8) enforced everywhere? Could a listing with 50 photos blow up API costs?
- **Frontend memory**: Loading 500+ listings with photo URLs into DOM — any performance concerns?
- **Map markers**: 500+ Leaflet markers without clustering?

### P3 Candidates
- **Code duplication**: Similar patterns in redfin_fetcher and zillow_fetcher that could share a base
- **Hardcoded values**: Magic numbers that should be in config.py
- **Missing indexes**: DB queries that would benefit from additional indexes
- **Test coverage**: 5 test files exist — are they sufficient? Any critical paths untested?
- **Logging**: Consistent use of logger vs print statements
- **Type hints**: Missing type annotations in function signatures

## Output Format

```markdown
## P0 — Critical
1. **[file:line]** Description of the issue. What breaks. How to fix.

## P1 — High
1. **[file:line]** Description...

## P2 — Medium
1. **[file:line]** Description...

## P3 — Low
1. **[file:line]** Description...

## Architecture Notes
- Any structural observations about modularity, coupling, or scalability
- Suggestions for the "extract to standalone service" path

## Summary
- Total issues: X (P0: N, P1: N, P2: N, P3: N)
- Overall assessment: [SHIP IT / FIX P0s FIRST / NEEDS WORK]
```

## Important Notes

- This is an **admin-only personal tool**, not customer-facing. Weight security findings accordingly — XSS in remarks text entered by the app owner is lower risk than XSS from user input.
- The DB is Railway Postgres accessed via `FSBO_DATABASE_URL` (or `DATABASE_URL` fallback). There is NO SQLAlchemy — all queries are raw psycopg2 with parameterized queries.
- The frontend is a **single standalone HTML file** served via Netlify. No build system, no React, no bundler. It uses Tailwind CDN, Leaflet CDN, and inline `<script>` tags.
- The module lives at `fsbo_tracker/` at the repo root — fully isolated from AVMLens. The ONLY cross-dependency is `api.services.geo_service.enrich_property()` called from `router.py`'s geo-enrich endpoint (returns 501 if unavailable). All other code is self-contained.
- `curl_cffi` is used for scraping (browser impersonation). This is intentional, not a bug.
