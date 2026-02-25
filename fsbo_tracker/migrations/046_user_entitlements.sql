-- 046: Product access model — entitlements, market gating, access log
-- Renames 'business' tier to 'pro', adds access control columns

-- Tier rename: business → pro (match new tier naming)
UPDATE fsbo_users SET tier = 'pro' WHERE tier = 'business';

-- Market gating
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS selected_market TEXT;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS market_selected_at TIMESTAMP;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS market_grace_used BOOLEAN DEFAULT FALSE;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS allowed_markets JSONB DEFAULT '[]';

-- Subscription tracking (for Phase 2 Helcim wiring)
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'none';
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS subscription_id TEXT;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS subscription_period_end TIMESTAMP;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS helcim_customer_code TEXT;

-- AI action daily limits
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS ai_actions_today INTEGER DEFAULT 0;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS ai_actions_reset_date DATE;

-- JWT version counter (bump to invalidate all existing tokens after tier change)
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS token_version INTEGER DEFAULT 0;

-- Access audit log (denials, redactions, billing dispute support)
CREATE TABLE IF NOT EXISTS fsbo_access_log (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT,
    action          TEXT NOT NULL,
    resource_id     TEXT,
    tier            TEXT,
    result          TEXT NOT NULL,
    detail          TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fsbo_access_log_user
    ON fsbo_access_log(user_id, created_at);
