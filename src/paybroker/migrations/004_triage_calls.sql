-- Every Tier 3 call that reached the LLM, so the daily cap and the "same device" grouping can be counted without scanning payments.
-- Idempotent, like 002. No card, e-mail, IP or device is stored: `transaction_id` is the internal payment row, and the identifiers
-- are read from that row when needed. There is deliberately no foreign key: a data reset deletes the payments but must not
-- hand back the day's token budget, and rows older than 7 days are purged by the worker.
CREATE TABLE IF NOT EXISTS triage_llm_call (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    transaction_id uuid NOT NULL,
    outcome        text NOT NULL CHECK (outcome IN ('ok', 'error'))   -- ok: a valid analysis came back; error: the model answered but it was unusable
);
CREATE INDEX IF NOT EXISTS triage_llm_call_at_idx ON triage_llm_call (at DESC);
