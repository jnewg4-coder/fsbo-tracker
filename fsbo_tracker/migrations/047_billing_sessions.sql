-- 047: Billing sessions + webhook event log
-- Tracks Helcim checkout lifecycle and webhook audit trail

-- Billing sessions: tracks checkout initialize → verify → active/failed lifecycle
-- UNIQUE on helcim_transaction_id enforces idempotency (single source of truth)
CREATE TABLE IF NOT EXISTS fsbo_billing_sessions (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    tier_id                 TEXT NOT NULL,
    amount_cents            INTEGER NOT NULL,
    status                  TEXT DEFAULT 'pending',
    helcim_checkout_token   TEXT,
    helcim_transaction_id   TEXT UNIQUE,
    helcim_card_token       TEXT,
    helcim_customer_code    TEXT,
    helcim_subscription_id  TEXT,
    created_at              TIMESTAMP DEFAULT NOW(),
    updated_at              TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fsbo_billing_user ON fsbo_billing_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_fsbo_billing_status ON fsbo_billing_sessions(status);

-- Webhook event log: audit trail ONLY, NOT used for idempotency gating
-- Idempotency is enforced by UNIQUE constraint on fsbo_billing_sessions.helcim_transaction_id
CREATE TABLE IF NOT EXISTS fsbo_webhook_events (
    id              SERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    transaction_id  TEXT,
    payload         JSONB NOT NULL,
    result          TEXT NOT NULL,
    detail          TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fsbo_webhook_tx ON fsbo_webhook_events(transaction_id);
