-- Who did what to the whole system, and when. Written by administrative actions that cannot be undone (today: the data reset).
-- Idempotent, like 002. `detail` never holds a secret: options and row counts only.
CREATE TABLE IF NOT EXISTS admin_audit (
    id     bigserial PRIMARY KEY,
    actor  text NOT NULL,
    action text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}',
    at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_audit_action_at_idx ON admin_audit (action, at DESC);
