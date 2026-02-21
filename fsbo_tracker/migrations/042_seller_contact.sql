-- Seller / owner contact info extracted from listings
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS seller_name TEXT;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS seller_phone TEXT;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS seller_email TEXT;
ALTER TABLE fsbo_listings ADD COLUMN IF NOT EXISTS seller_broker TEXT;
