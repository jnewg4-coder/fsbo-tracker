-- 056: Advisor add-on billing columns
-- Tracks Helcim add-on linkage (add-on ID 1804 linked to tier subscriptions)

ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_addon_id INTEGER;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_addon_status TEXT DEFAULT 'none';
-- Values: 'none', 'active', 'cancelled'

ALTER TABLE fsbo_billing_sessions ADD COLUMN IF NOT EXISTS include_advisor BOOLEAN DEFAULT false;
