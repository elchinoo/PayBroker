-- Row MACs for LLM accounts and the behaviour settings (see rowmac.py), and merchant.webhook_secret_enc joins the merchant MAC.
-- Null on rows written before this migration: once the vault is open they are ignored until `paybroker adopt-rowmacs` signs them
-- (the merchant rows of migration 009 need the same step, because their signed columns changed).
ALTER TABLE llm_account ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE app_setting ADD COLUMN IF NOT EXISTS mac text;
