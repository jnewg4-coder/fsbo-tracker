-- Add rent_zestimate column for Zillow rent estimates
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fsbo_listings' AND column_name = 'rent_zestimate'
    ) THEN
        ALTER TABLE fsbo_listings ADD COLUMN rent_zestimate INTEGER;
    END IF;
END $$;
