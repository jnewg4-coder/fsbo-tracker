-- SELL pipeline v2: TC workflow columns
-- Most task statuses live in workflow_state JSON; these are key financial/search fields

-- Property management
ALTER TABLE deals ADD COLUMN IF NOT EXISTS property_manager TEXT;

-- BPO / Turn
ALTER TABLE deals ADD COLUMN IF NOT EXISTS bpo_price NUMERIC(12,2);
ALTER TABLE deals ADD COLUMN IF NOT EXISTS turn_cost NUMERIC(12,2);

-- Pricing worksheet
ALTER TABLE deals ADD COLUMN IF NOT EXISTS floor_price NUMERIC(12,2);
ALTER TABLE deals ADD COLUMN IF NOT EXISTS acquisition_basis NUMERIC(12,2);
ALTER TABLE deals ADD COLUMN IF NOT EXISTS required_return_pct NUMERIC(5,2);

-- MLS / Marketing
ALTER TABLE deals ADD COLUMN IF NOT EXISTS mls_number TEXT;

-- Showings & Offers
ALTER TABLE deals ADD COLUMN IF NOT EXISTS showing_count INTEGER DEFAULT 0;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS accepted_offer_price NUMERIC(12,2);

-- Buyer info
ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_name TEXT;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_agent_name TEXT;

-- Commission
ALTER TABLE deals ADD COLUMN IF NOT EXISTS commission_pct NUMERIC(5,2);

-- Post-close
ALTER TABLE deals ADD COLUMN IF NOT EXISTS proceeds_received_date DATE;
