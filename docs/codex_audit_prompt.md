# FSBO Tracker — Codex Audit Prompt

## Scope

Audit all code in this repo (`fsbo-tracker/`). Focus on the 12 commits since initial deploy (see git log). The frontend is a single-file SPA (`frontend/listing-tracker.html`) and the backend is a FastAPI app (`fsbo_tracker/`).

## Priority 1: MAP GEO LAYERS DO NOT RENDER

**This is the main reason for the audit.** The map (Leaflet, CARTO dark tiles) shows property markers correctly but **geo risk layers (Superfund, highway, railroad, etc.) have NEVER displayed** despite:
- Geo data being fetched successfully (API returns factors with lat/lon)
- `geoCache` being populated (cards show geo tags correctly)
- Layer toggle bar rendering (buttons appear, can click All)
- `addGeoLayerToMap()` being called via `refreshActiveGeoLayers()`

### Key files and line numbers:
- **`frontend/listing-tracker.html`**:
  - `addGeoLayerToMap()` ~line 2474 — creates `L.layerGroup()`, iterates `geoCache`, adds polylines/markers
  - `refreshActiveGeoLayers()` ~line 2523 — called after geo data fetched
  - `renderMap()` ~line 1393 — creates Leaflet map with CARTO dark tiles
  - `autoGeoEnrich()` ~line 755 — auto-fetches geo for first 10 listings
  - `GEO_LAYER_STYLES` ~line 1872 — layer colors/icons

### Known issue:
- `geo_lite.py` returns **only point coordinates** (`lat`, `lon`) per factor — NO `geometry` array (no polyline paths)
- `addGeoLayerToMap()` checks `f.geometry && f.geometry.length > 1` for polylines — this will never be true
- Point markers check `f.lat && f.lon` — these values DO exist in the response
- **So why aren't even the point markers showing?** This needs investigation.

### Questions to answer:
1. Is `maps['search-map']` initialized before `addGeoLayerToMap()` runs?
2. Is the `L.layerGroup().addTo(map)` actually executing? Are there JS console errors?
3. Does the `bg-transparent border-0` class on `L.divIcon` work with Tailwind CDN? (Tailwind CDN generates utility classes — `bg-transparent` may not be in the default set)
4. Are the `lat`/`lon` values in geoCache factors valid numbers or strings?
5. Is there a race condition between `autoGeoEnrich()` completing and `refreshActiveGeoLayers()` running?
6. Check the Leaflet z-index stacking — are geo layers behind the tile layer?

### Geo API response shape (from `geo_lite.py`):
```json
{
  "listing_id": "abc123",
  "success": true,
  "factors": [
    {
      "layer": "highway",
      "distance_mi": 0.23,
      "adjustment_pct": -7.5,
      "details": "I-485",
      "lat": 35.1234,
      "lon": -80.5678
    }
  ],
  "total_adjustment_pct": -12.3,
  "risk_level": "MODERATE"
}
```

Note: `geometry` field (polyline paths) is NOT returned by `geo_lite.py`. The `addGeoLayerToMap()` code expects it but it doesn't exist. Point markers should still work via `f.lat` and `f.lon`.

---

## Priority 2: Frontend Card Layout Audit

Multiple rapid iterations on card layout. Verify:

### Current card structure (right column):
1. Address row: clickable link + score number + heart + archive
2. Price row: $price, $/sf, sqft, cuts, ratio, badges, keyword badges, RF/ZL links
3. Financial grid: 7 rows × 2 columns with 1px vertical divider
   - Left: Est Purchase, Offer to List, ARV, Reno, Basis, Profit, ROI
   - Right: Rent AVM, Rent Price, Taxes, HOA, Rent % of Basis, Gross Yield, Max Offer
4. Geo tags (under financials)

### Current card structure (left column, under photo):
1. Photo (140×105px, clickable)
2. Source · DOM · beds/baths
3. Buttons: Rmks, Notes, AI, Geo
4. Status dropdown

### Check:
- [ ] Score hover tooltip shows breakdown (KW/Photo/Price/DOM/Cuts)
- [ ] Vertical divider (1px `#374151`) renders between financial columns
- [ ] `calculateCardMetrics()` returns correct values (especially `rentPctOfBasis`)
- [ ] Financial values don't show `NaN` or `$NaN` for edge cases (null price, 0 sqft, etc.)
- [ ] Division by zero guards on all percentage calculations
- [ ] Cards scroll in the `overflow-y: auto` container (inline `height:82vh`)
- [ ] Photo modal z-index (1050) above Leaflet map panes

---

## Priority 3: Multi-Select Filter Dropdowns

Two new filter dropdowns added (Type, Status). Verify:

- [ ] `ACTIVE_TYPES` and `ACTIVE_STATUSES` Sets filter correctly in `runSearch()`
- [ ] Type filter: counts per type update correctly
- [ ] Status filter: reads from `propertyStatuses[l.id]` (localStorage) correctly
- [ ] "All" option clears the set and shows all
- [ ] Dropdowns close on outside click
- [ ] `.has-selection` CSS class applies when filters active
- [ ] No conflict with existing market dropdown click handler

---

## Priority 4: Scheduler

`fsbo_tracker/run.py` — `_run_scheduled()`:
- [ ] Runs once daily, random between 8:00am-9:59am EST
- [ ] EST timezone is `timezone(timedelta(hours=-5))` — this does NOT handle DST (EDT = -4). During EDT (March-November), runs will be at 9:00am-10:59am EDT. Decide if this is acceptable or use `zoneinfo.ZoneInfo("America/New_York")`.
- [ ] If target time already passed today, schedules for tomorrow
- [ ] `_time.sleep()` minimum of 60 seconds (prevent tight loop)

---

## Priority 5: Backend / Security

- [ ] All router endpoints require `X-Admin-Password` header (via `Depends(verify_fsbo_admin)`)
- [ ] `db_cursor` context manager used correctly (no leaked connections)
- [ ] `geo_lite.py` — API calls have timeouts, handle network errors
- [ ] `proxy.py` — no credential leaks in logs (password masked in proxy URL)
- [ ] `.env.example` has no real secrets
- [ ] `requirements.txt` has pinned or reasonable versions

---

## Files to Review

| File | What |
|------|------|
| `frontend/listing-tracker.html` | Single-page app (HTML + CSS + JS, ~2600 lines) |
| `fsbo_tracker/router.py` | FastAPI endpoints |
| `fsbo_tracker/geo_lite.py` | Standalone geo proximity module |
| `fsbo_tracker/proxy.py` | Proxy session manager (IPRoyal + OxyLabs) |
| `fsbo_tracker/run.py` | CLI entry point + scheduler |
| `fsbo_tracker/tracker.py` | Pipeline orchestrator |
| `fsbo_tracker/db.py` | Database queries |
| `fsbo_tracker/scorer.py` | Listing scoring logic |
| `fsbo_tracker/photo_analyzer.py` | Haiku vision analysis |
| `fsbo_tracker/app.py` | FastAPI app + CORS config |

## Running Locally

```bash
# Backend
export FSBO_DATABASE_URL='postgresql://...'
export ADMIN_PASSWORD='...'
uvicorn fsbo_tracker.app:app --port 8100 --reload

# Frontend
cd frontend && python -m http.server 8888

# Test geo-enrich
curl -X POST http://localhost:8100/api/v2/fsbo/listings/LISTING_ID/geo-enrich \
  -H "X-Admin-Password: $ADMIN_PASSWORD"
```

## Production URLs
- **API**: https://fsbo-api-production.up.railway.app
- **Frontend**: https://fsbo-tracker.netlify.app
