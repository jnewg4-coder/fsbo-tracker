-- Deal Pipeline tables — transaction coordination from Offer through Closing
-- Supports dual-sided (BUY/SELL) via side + stage_profile columns

-- ── deals ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id      TEXT,                       -- FK to fsbo_listings.id (nullable for manual deals)
    side            TEXT NOT NULL DEFAULT 'BUY', -- BUY or SELL
    stage_profile   TEXT NOT NULL DEFAULT 'buy_v1', -- buy_v1 or sell_v1

    -- Property basics (carried from listing or entered manually)
    address         TEXT NOT NULL,
    city            TEXT,
    state           TEXT,
    zip_code        TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    beds            INTEGER,
    baths           NUMERIC(4,1),
    sqft            INTEGER,
    year_built      INTEGER,
    property_type   TEXT,

    -- Valuations carried over from listing
    list_price      NUMERIC(12,2),
    assessed_value  NUMERIC(12,2),
    zestimate       NUMERIC(12,2),
    redfin_estimate NUMERIC(12,2),
    flood_zone      TEXT,
    photo_urls      TEXT,                       -- JSON array
    photo_analysis_json TEXT,                   -- AI photo analysis snapshot
    geo_risk_json   TEXT,                       -- Geo risk snapshot
    source_links    TEXT,                       -- JSON: {redfin_url, zillow_url}

    -- Seller / buyer info carried over
    seller_name     TEXT,
    seller_phone    TEXT,
    seller_email    TEXT,
    seller_broker   TEXT,

    -- Stage tracking
    stage           TEXT NOT NULL DEFAULT 'offer',
    stage_changed_at TIMESTAMP DEFAULT NOW(),

    -- Offer stage
    offer_price             NUMERIC(12,2),
    offer_date              DATE,
    offer_expiration_date   DATE,
    contingencies           TEXT,               -- JSON array
    emd_amount              NUMERIC(12,2),
    emd_due_date            DATE,
    acceptance_date         DATE,

    -- Contract stage
    contract_signed_date    DATE,
    binding_date            DATE,
    bind_notice_sent        BOOLEAN DEFAULT FALSE,
    bind_notice_date        DATE,
    disclosures_received    BOOLEAN DEFAULT FALSE,
    disclosures_date        DATE,

    -- Title stage
    title_company           TEXT,
    title_officer_name      TEXT,
    title_officer_phone     TEXT,
    title_officer_email     TEXT,
    title_ordered_date      DATE,
    title_received_date     DATE,
    survey_ordered_date     DATE,
    survey_received_date    DATE,

    -- Due Diligence stage
    dd_period_days          INTEGER,
    dd_start_date           DATE,
    dd_end_date             DATE,
    dd_status               TEXT,               -- clear, issue, retrade_needed
    ccr_review_status       TEXT,

    -- Retrade stage
    retrade_requested       BOOLEAN DEFAULT FALSE,
    retrade_date            DATE,
    original_price          NUMERIC(12,2),
    retrade_price           NUMERIC(12,2),
    credit_requested        NUMERIC(12,2),
    retrade_items           TEXT,               -- JSON array of {item, cost_est, severity}
    retrade_status          TEXT,               -- pending, accepted, countered, rejected
    retrade_counter_price   NUMERIC(12,2),

    -- Clear to Close stage
    clear_to_close_date     DATE,
    final_walkthrough_date  DATE,
    final_walkthrough_status TEXT,
    hud_review_status       TEXT,
    deed_review_status      TEXT,
    wire_instructions_received BOOLEAN DEFAULT FALSE,
    cash_due_at_close       NUMERIC(12,2),

    -- Closed stage
    closing_date            DATE,
    final_purchase_price    NUMERIC(12,2),
    total_closing_costs     NUMERIC(12,2),
    deed_recorded           BOOLEAN DEFAULT FALSE,
    deed_recorded_date      DATE,
    alta_received           BOOLEAN DEFAULT FALSE,
    final_hud_received      BOOLEAN DEFAULT FALSE,
    all_docs_clear          BOOLEAN DEFAULT FALSE,

    -- Meta
    notes           TEXT,
    tags            TEXT,                       -- JSON array
    tier            TEXT NOT NULL DEFAULT 'paid',
    archived        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals (stage);
CREATE INDEX IF NOT EXISTS idx_deals_side ON deals (side);
CREATE INDEX IF NOT EXISTS idx_deals_listing_id ON deals (listing_id);
CREATE INDEX IF NOT EXISTS idx_deals_archived ON deals (archived);

-- Prevent duplicate active deals for the same listing (race condition guard)
CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_listing_unique_active
    ON deals (listing_id) WHERE listing_id IS NOT NULL AND archived = FALSE;

-- ── deal_contacts ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deal_contacts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id     UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                  -- seller, buyer_agent, attorney, title_officer, inspector, lender, contractor
    name        TEXT,
    phone       TEXT,
    email       TEXT,
    company     TEXT,
    notes       TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deal_contacts_deal ON deal_contacts (deal_id);

-- ── deal_documents (BYTEA storage) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deal_documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id         UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    stage           TEXT,                       -- which stage this doc belongs to
    doc_type        TEXT NOT NULL,              -- contract, inspection_report, appraisal, title_commitment, survey, hud, deed, amendment, photo, other
    filename        TEXT NOT NULL,
    mime_type       TEXT,
    file_size       INTEGER,
    file_data       BYTEA,
    ai_analysis_json TEXT,                      -- AI analysis results
    uploaded_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deal_documents_deal ON deal_documents (deal_id);
CREATE INDEX IF NOT EXISTS idx_deal_documents_type ON deal_documents (doc_type);

-- ── deal_inspections ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deal_inspections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id             UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    inspection_type     TEXT NOT NULL,          -- scope, home, termite, septic, radon, other
    inspector_name      TEXT,
    inspector_phone     TEXT,
    inspector_email     TEXT,
    inspector_company   TEXT,
    ordered_date        DATE,
    completed_date      DATE,
    status              TEXT DEFAULT 'pending', -- pending, scheduled, completed, cancelled
    report_doc_id       UUID REFERENCES deal_documents(id) ON DELETE SET NULL,
    findings_json       TEXT,                   -- AI-extracted findings
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deal_inspections_deal ON deal_inspections (deal_id);

-- ── deal_activity_log (append-only audit) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS deal_activity_log (
    id          BIGSERIAL PRIMARY KEY,
    deal_id     UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    action      TEXT NOT NULL,                  -- stage_change, field_update, doc_upload, contact_add, etc.
    detail      TEXT,
    old_value   TEXT,
    new_value   TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deal_activity_deal ON deal_activity_log (deal_id);
CREATE INDEX IF NOT EXISTS idx_deal_activity_created ON deal_activity_log (created_at);

-- ── offer_drafts (AI-generated offer letters) ────────────────────────────────
CREATE TABLE IF NOT EXISTS offer_drafts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id         UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    draft_type      TEXT NOT NULL DEFAULT 'purchase_offer', -- purchase_offer, counter_offer, amendment
    inputs_json     TEXT,                       -- snapshot of deal data used as AI input
    output_md       TEXT,                       -- generated offer in markdown
    model           TEXT,                       -- which AI model generated it
    model_version   TEXT,                       -- specific model version string
    status          TEXT DEFAULT 'draft',       -- draft, reviewed, approved, sent, rejected
    generated_by    TEXT,                       -- 'ai' or user identifier
    approved_by     TEXT,                       -- who approved the draft
    approved_at     TIMESTAMP,                  -- when approved (REQUIRED before send/export)
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_offer_drafts_deal ON offer_drafts (deal_id);
