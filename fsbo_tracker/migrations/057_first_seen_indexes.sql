-- Migration 057: Indexes for first_seen_at queries
-- Supports market-count endpoint (status + first_seen_at) and stale pipeline check (first_seen_at)

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fsbo_listings_status_first_seen
    ON fsbo_listings(status, first_seen_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fsbo_listings_first_seen_desc
    ON fsbo_listings(first_seen_at DESC);
