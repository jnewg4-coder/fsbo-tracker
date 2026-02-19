-- Add last sold price/date for price history tracking
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fsbo_listings' AND column_name = 'last_sold_price'
    ) THEN
        ALTER TABLE fsbo_listings ADD COLUMN last_sold_price INTEGER;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fsbo_listings' AND column_name = 'last_sold_date'
    ) THEN
        ALTER TABLE fsbo_listings ADD COLUMN last_sold_date TEXT;
    END IF;
END $$;
