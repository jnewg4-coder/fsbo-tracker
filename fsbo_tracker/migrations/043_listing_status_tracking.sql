-- Track source-reported listing status (Active, Pending, Contingent, Sold)
-- and auto-archive sold listings separately from user archive

ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS listing_status TEXT DEFAULT 'Active';
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMP;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS sold_price INTEGER;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS sold_date DATE;

-- Index for querying under_contract / sold statuses
CREATE INDEX IF NOT EXISTS idx_fsbo_listings_listing_status ON fsbo_listings(listing_status);
