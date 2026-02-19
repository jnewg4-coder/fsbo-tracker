-- FSBO Listing Tracker tables (standalone from AVMLens)

CREATE TABLE IF NOT EXISTS fsbo_searches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    region_id   INTEGER,
    min_lat     REAL, max_lat REAL,
    min_lng     REAL, max_lng REAL,
    max_price   INTEGER DEFAULT 500000,
    min_beds    INTEGER DEFAULT 2,
    min_dom     INTEGER DEFAULT 55,
    grace_days  INTEGER DEFAULT 3,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fsbo_listings (
    id                  TEXT PRIMARY KEY,
    search_id           TEXT REFERENCES fsbo_searches(id),
    source              TEXT DEFAULT 'redfin',
    address             TEXT NOT NULL,
    city                TEXT,
    state               TEXT,
    zip_code            TEXT,
    latitude            REAL,
    longitude           REAL,
    listing_type        TEXT,
    price               INTEGER,
    beds                REAL,
    baths               REAL,
    sqft                INTEGER,
    year_built          INTEGER,
    property_type       TEXT,
    dom                 INTEGER,
    days_seen           INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'active',
    score               INTEGER DEFAULT 0,
    score_breakdown     TEXT,
    keywords_matched    TEXT,
    remarks             TEXT,
    photo_urls          TEXT,
    photo_damage_score  INTEGER,
    photo_damage_notes  TEXT,
    photo_analysis_json TEXT,
    photo_analyzed_at   TIMESTAMP,
    assessed_value      INTEGER,
    redfin_estimate     INTEGER,
    price_cuts          INTEGER DEFAULT 0,
    last_price_cut_pct  REAL,
    last_price_cut_at   TIMESTAMP,
    first_seen_at       TIMESTAMP DEFAULT NOW(),
    last_seen_at        TIMESTAMP DEFAULT NOW(),
    gone_at             TIMESTAMP,
    grace_until         TIMESTAMP,
    redfin_url          TEXT,
    zillow_url          TEXT,
    detail_fetched_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fsbo_price_events (
    id           SERIAL PRIMARY KEY,
    listing_id   TEXT REFERENCES fsbo_listings(id),
    price_before INTEGER,
    price_after  INTEGER,
    change_pct   REAL,
    detected_at  TIMESTAMP DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_fsbo_listings_status ON fsbo_listings(status);
CREATE INDEX IF NOT EXISTS idx_fsbo_listings_search ON fsbo_listings(search_id);
CREATE INDEX IF NOT EXISTS idx_fsbo_listings_score ON fsbo_listings(score DESC);
CREATE INDEX IF NOT EXISTS idx_fsbo_price_events_listing ON fsbo_price_events(listing_id);
