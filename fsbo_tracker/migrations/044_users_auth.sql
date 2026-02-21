-- User accounts and authentication for FSBO Tracker
-- Replaces X-Admin-Password with JWT-based auth

CREATE TABLE IF NOT EXISTS fsbo_users (
    id                    TEXT PRIMARY KEY,
    email                 TEXT UNIQUE NOT NULL,
    password_hash         TEXT NOT NULL,
    role                  TEXT DEFAULT 'user',      -- 'admin', 'user', 'viewer'
    tier                  TEXT DEFAULT 'free',       -- 'free', 'starter', 'pro', 'business'
    is_active             BOOLEAN DEFAULT TRUE,
    failed_login_attempts INTEGER DEFAULT 0,
    locked_until          TIMESTAMP,
    last_login_at         TIMESTAMP,
    created_at            TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fsbo_users_email ON fsbo_users(email);
