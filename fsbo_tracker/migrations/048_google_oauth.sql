-- Add Google OAuth support to fsbo_users
-- Allow NULL password_hash (Google OAuth users don't have passwords)
ALTER TABLE fsbo_users ALTER COLUMN password_hash DROP NOT NULL;

-- Google OAuth columns
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS google_id TEXT;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS google_picture TEXT;

-- Unique index on google_id (partial — only non-null)
CREATE UNIQUE INDEX IF NOT EXISTS idx_fsbo_users_google_id
    ON fsbo_users(google_id) WHERE google_id IS NOT NULL;
