-- Migration 041: Persist FEMA flood zone summary per listing

ALTER TABLE fsbo_listings
    ADD COLUMN IF NOT EXISTS flood_zone TEXT;

ALTER TABLE fsbo_listings
    ADD COLUMN IF NOT EXISTS flood_risk_level TEXT;
