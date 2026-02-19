-- Seller / owner contact info extracted from listings
ALTER TABLE fsbo_listings ADD COLUMN seller_name TEXT;
ALTER TABLE fsbo_listings ADD COLUMN seller_phone TEXT;
ALTER TABLE fsbo_listings ADD COLUMN seller_email TEXT;
ALTER TABLE fsbo_listings ADD COLUMN seller_broker TEXT;
