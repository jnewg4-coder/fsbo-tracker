-- Email verification + password reset columns
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS verification_code TEXT;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS verification_expires_at TIMESTAMP;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS password_reset_token TEXT;
ALTER TABLE fsbo_users ADD COLUMN IF NOT EXISTS password_reset_expires_at TIMESTAMP;
