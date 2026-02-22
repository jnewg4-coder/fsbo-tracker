-- 045: NDVI vegetation columns for FSBO listings
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS ndvi_mean REAL;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS ndvi_overgrowth_level TEXT;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS ndvi_overgrowth_pct REAL;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS ndvi_capture_year INTEGER;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS ndvi_confidence TEXT;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS ndvi_checked_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_fsbo_ndvi_level
    ON fsbo_listings(ndvi_overgrowth_level) WHERE ndvi_overgrowth_level IS NOT NULL;
