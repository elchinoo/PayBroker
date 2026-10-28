-- Row-level security as defence in depth: the database, not only the application's WHERE clauses, keeps one merchant's rows from
-- another merchant's request. Core Postgres only.
--
-- Who sees what is decided by two session settings that the application sets per connection checkout (paybroker/db.py):
--   app.merchant_id = 'mch_...'   a merchant's request (its API key): that merchant's rows only
--   app.bypass      = 'on'        the operator dashboard, the worker, the CLI and the hosted 3-D Secure page (acting for the issuer)
-- Anything else, including a connection that set neither, sees NO rows and can insert none: it fails closed.
-- The policies are FORCEd, so they bind the table owner too (the app connects as the owner). A superuser or a role with BYPASSRLS
-- is exempt from RLS by Postgres design; GET /api/audit/verify reports whether the connected role is one.
--
-- Merchant-scoped (own rows only):  merchant, merchant_key, card, transaction, webhook_event, and their children
--                                   three_ds_challenge and fraud_assessment (through the payment they belong to).
-- Operator-only (no merchant ever): fraud_case, chargeback, admin_audit.
-- Deliberately NOT under RLS (shared by design, listed in docs/GUIDE.md section 18): the fraud feature store (it links customers,
-- devices and cards ACROSS merchants: that is what finds fraud rings), outbox, training_example, rules, blocklist, connectors,
-- models, settings, and the audit ledger (a merchant request never reads it; its payloads are masked).

-- The read-only group role of AgentPG (a1p_readonly, and the masked views that run for it) is let in for SELECT, but only a session
-- that USES its privileges (USAGE: a real, inherited membership, not just the ADMIN option a role creator gets) and does NOT also use
-- a1p_readwrite's: where readwrite inherits readonly, the application's own role must not slip through this door.
CREATE FUNCTION pb_readonly_session() RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT COALESCE(pg_has_role(session_user, to_regrole('a1p_readonly'), 'USAGE'), false)
       AND NOT COALESCE(pg_has_role(session_user, to_regrole('a1p_readwrite'), 'USAGE'), false)
$$;

-- "Is the payment this child row belongs to visible to this session?" It runs as the table owner (SECURITY DEFINER) because the read-only
-- role has no privilege on `transaction` (it reads a masked view), and a policy that names a table the reader may not touch would make the
-- query fail instead of filtering. A definer is exempt from RLS when it is a superuser, so the function does not lean on transaction's own
-- policy: it repeats the same test (same settings, which are session level and therefore the same inside the function).
CREATE FUNCTION pb_payment_visible(p_transaction uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.transaction t
        WHERE t.id = p_transaction
          AND (COALESCE(current_setting('app.bypass', true), '') = 'on' OR t.merchant_id = current_setting('app.merchant_id', true)))
$$;

CREATE INDEX IF NOT EXISTS card_merchant_idx ON card (merchant_id);
CREATE INDEX IF NOT EXISTS merchant_key_merchant_idx ON merchant_key (merchant_id);

DO $$
DECLARE
    t text;
    -- (SELECT ...) wrappers make Postgres evaluate each setting once per statement (an InitPlan), not once per row.
    bypass constant text := '(SELECT current_setting(''app.bypass'', true) = ''on'')';
    mine   constant text := '(SELECT current_setting(''app.merchant_id'', true))';
    ro     constant text := '(SELECT pb_readonly_session())';
BEGIN
    FOREACH t IN ARRAY ARRAY['merchant_key', 'card', 'transaction', 'webhook_event'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY pb_scope ON %I USING (%s OR merchant_id = %s)', t, bypass, mine);
        EXECUTE format('CREATE POLICY pb_readonly ON %I FOR SELECT USING (%s)', t, ro);
    END LOOP;

    ALTER TABLE merchant ENABLE ROW LEVEL SECURITY;
    ALTER TABLE merchant FORCE ROW LEVEL SECURITY;
    EXECUTE format('CREATE POLICY pb_scope ON merchant USING (%s OR id = %s)', bypass, mine);
    EXECUTE format('CREATE POLICY pb_readonly ON merchant FOR SELECT USING (%s)', ro);

    -- children: visible when the payment they belong to is (pb_payment_visible repeats transaction's policy)
    FOREACH t IN ARRAY ARRAY['three_ds_challenge', 'fraud_assessment'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY pb_scope ON %I USING (%s OR pb_payment_visible(transaction_id))', t, bypass);
        EXECUTE format('CREATE POLICY pb_readonly ON %I FOR SELECT USING (%s)', t, ro);
    END LOOP;

    -- operator-only
    FOREACH t IN ARRAY ARRAY['fraud_case', 'chargeback', 'admin_audit'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY pb_scope ON %I USING (%s)', t, bypass);
        EXECUTE format('CREATE POLICY pb_readonly ON %I FOR SELECT USING (%s)', t, ro);
    END LOOP;

    -- ... except that the payment pipeline, running for a merchant, OPENS a case when it scores a payment high: insert only, and only for
    -- a payment that merchant owns. It cannot read a case back.
    CREATE POLICY pb_open_case ON fraud_case FOR INSERT WITH CHECK (pb_payment_visible(transaction_id));
END
$$;
