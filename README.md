# FSBO Deal Tracker — Off-Market FSBO Intelligence

**[fsbotracker.app](https://fsbotracker.app)** | Distress-scored FSBO listings across 38 US metros

FSBO Deal Tracker scans For Sale By Owner listings daily, scores them for investor distress signals, and surfaces the highest-priority deals before they hit the MLS.

## What It Does

- **Automated Distress Scoring (0-100)** — Five signal categories: keyword analysis (motivated seller language), photo AI damage detection, price-to-assessed ratio, days on market, and price cut history.
- **38 US Metro Markets** — Charlotte, Nashville, Tampa, Atlanta, Houston, Dallas, Philadelphia, Detroit, Phoenix, Denver, Las Vegas, Salt Lake City, and 26 more. Daily scans with deduplication.
- **Saved Search Alerts** — Define criteria (market, score threshold, price range, listing type) and get email alerts when matching listings appear.
- **NDVI Overgrowth Detection** — Satellite imagery analysis flags properties with vegetation overgrowth, a key distress indicator.
- **Deal Pipeline** — BUY (7 stages) and SELL (9 stages) workflow tracking from offer through closing.
- **AI Deal Advisor** — On-demand AI analysis of individual listings with voice personas and expert context.

## Who Uses It

- Wholesalers finding motivated FSBO sellers for direct outreach
- Fix-and-flip investors screening for below-market distressed properties
- Buy-and-hold investors identifying rental acquisition targets
- Transaction coordinators managing FSBO deal workflows

## Scoring Breakdown

| Signal | Max Points | Method |
|--------|-----------|--------|
| Keywords in remarks | 40 | 22 regex patterns across 3 severity tiers |
| Photo AI analysis | 25 | Computer vision for deferred maintenance |
| Price/assessed ratio | 20 | Below-assessed = higher score |
| Days on market | 10 | Longer DOM = higher motivation signal |
| Price cuts | 5 | Frequency and magnitude of reductions |

Shortlist threshold: 35+. High priority: 60+.

## Architecture

FastAPI backend on Railway, static frontend on Cloudflare Pages. PostgreSQL for listings/users/alerts. Redfin GIS-CSV + Zillow data sources with proxy rotation and circuit breakers.

## Related

- **[AVMLens](https://avmlens.app)** — Multi-source AVM aggregation with geo risk scoring for property valuations

## Local Development

```bash
# API
cd fsbo_tracker && uvicorn app:app --reload --port 8001

# Frontend
cd frontend && python -m http.server 3001
```

Requires `FSBO_DATABASE_URL` environment variable for PostgreSQL connection.
