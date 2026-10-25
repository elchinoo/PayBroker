-- Browser access for a merchant's web shop: the exact origins allowed to call the publishable-key tokenization endpoint from a customer's
-- browser (CORS), and the return URL a 3-D Secure challenge sends the customer back to. Idempotent, like 002.
-- `allowed_origins` holds scheme://host[:port] strings, validated and normalized by merchants.clean_origins before they are stored.
ALTER TABLE merchant ADD COLUMN IF NOT EXISTS allowed_origins text[] NOT NULL DEFAULT '{}';
ALTER TABLE three_ds_challenge ADD COLUMN IF NOT EXISTS return_url text;   -- NULL: the hosted page just shows the result, as before
