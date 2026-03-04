-- Migration 058: Composite index for fresh-listing count query (status + dom)
-- Supports market-count endpoint: WHERE status = 'active' AND dom <= 5

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fsbo_listings_status_dom
    ON fsbo_listings(status, dom);
