-- Row MACs: an HMAC under a subkey of the vault data key over the columns that decide authority (see rowmac.py).
-- Null on rows written before this migration: once the vault is open they are ignored until `paybroker adopt-rowmacs` signs them.
ALTER TABLE merchant ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE merchant_key ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE psp_connector ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE fraud_rule ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE blacklist ADD COLUMN IF NOT EXISTS mac text;
