-- Saved searches, notifications, and AI advisor tables
-- All IDs are TEXT (app-generated UUIDs), matching fsbo_users.id pattern

-- Saved searches with criteria-based matching
CREATE TABLE IF NOT EXISTS fsbo_saved_searches (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES fsbo_users(id),
    name TEXT NOT NULL,
    markets JSONB NOT NULL DEFAULT '[]',
    min_score INTEGER DEFAULT 0,
    max_price INTEGER,
    min_price INTEGER,
    listing_types JSONB DEFAULT '["fsbo","mlsfsbo"]',
    status_filter TEXT DEFAULT 'active',
    min_dom INTEGER,
    ndvi_levels JSONB,
    custom_keywords JSONB,
    created_via TEXT DEFAULT 'form',          -- 'form' or 'advisor'
    ai_prompt TEXT,                           -- original NL if advisor-created
    is_active BOOLEAN DEFAULT true,
    last_checked_at TIMESTAMPTZ,             -- cursor for incremental matching
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fsbo_saved_searches_user
    ON fsbo_saved_searches(user_id);
CREATE INDEX IF NOT EXISTS idx_fsbo_saved_searches_active
    ON fsbo_saved_searches(is_active) WHERE is_active = true;

-- Dedup: one notification per search+listing pair
CREATE TABLE IF NOT EXISTS fsbo_notification_matches (
    search_id TEXT NOT NULL REFERENCES fsbo_saved_searches(id) ON DELETE CASCADE,
    listing_id TEXT NOT NULL,
    matched_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (search_id, listing_id)
);

-- User notification preferences
CREATE TABLE IF NOT EXISTS fsbo_notification_prefs (
    user_id TEXT PRIMARY KEY REFERENCES fsbo_users(id),
    email_enabled BOOLEAN DEFAULT true,
    delivery_schedule TEXT DEFAULT 'daily_9am',   -- immediate, daily_9am, daily_12pm, daily_6pm
    timezone TEXT DEFAULT 'America/New_York',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Notification dispatch log
CREATE TABLE IF NOT EXISTS fsbo_notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES fsbo_users(id),
    saved_search_id TEXT REFERENCES fsbo_saved_searches(id),
    listing_ids JSONB NOT NULL,
    channel TEXT NOT NULL DEFAULT 'email',
    status TEXT DEFAULT 'pending',                -- pending, sent, failed
    scheduled_for TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    content_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fsbo_notifications_pending
    ON fsbo_notifications(status, scheduled_for)
    WHERE status = 'pending';

-- Advisor conversation messages (individual rows, not JSON blob)
CREATE TABLE IF NOT EXISTS fsbo_advisor_messages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES fsbo_users(id),
    role TEXT NOT NULL,                           -- 'user', 'assistant', 'system'
    content TEXT NOT NULL,
    tool_calls JSONB,
    tool_results JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fsbo_advisor_messages_user
    ON fsbo_advisor_messages(user_id, created_at);

-- Advisor add-on columns on fsbo_users
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_enabled BOOLEAN DEFAULT false;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_messages_used INTEGER DEFAULT 0;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_messages_limit INTEGER DEFAULT 0;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_reset_date DATE;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS advisor_subscription_id TEXT;
