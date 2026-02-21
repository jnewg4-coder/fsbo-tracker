-- Add workflow_state JSON column for sub-task status tracking
-- Each stage has grouped workflow items with status dropdowns (stored as JSON)
-- Existing columns (dd_status, retrade_status, etc.) remain for advance-gate logic
-- workflow_state is purely for management/display tracking

ALTER TABLE deals ADD COLUMN IF NOT EXISTS workflow_state TEXT DEFAULT '{}';

-- Convert existing text fields to proper status tracking
-- (These were plain text inputs but should be dropdown-managed)
-- final_walkthrough_status, hud_review_status, deed_review_status, ccr_review_status
-- Already exist as TEXT columns — no migration needed, just frontend dropdown rendering
