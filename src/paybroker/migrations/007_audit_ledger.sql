-- Tamper-evident audit ledger: an append-only hash chain, written in the SAME transaction as the thing it records.
-- Only core Postgres: sha256(), jsonb (whose text form is canonical: sorted keys, normalised numbers), a head row that serialises
-- the writers, triggers and privileges. No pgcrypto, no pgaudit, no superuser.
--
--   hash(n) = sha256( hash(n-1) || canonical(n) )          hash(0) = 64 zeros
--   canonical(n) = jsonb array [seq, ts (UTC, microseconds), actor, action, subject, payload] as text
--
-- Who can still do what: nobody can UPDATE, DELETE or TRUNCATE through normal SQL (privileges REVOKEd, trigger raises). The table
-- OWNER (or a superuser) can switch a trigger off or re-GRANT; that is not prevented, it is DETECTED: audit_verify() recomputes
-- the chain and names the first entry that does not match. Idempotent where a rerun is plausible (the runner applies it once).

CREATE TABLE audit_ledger (
    seq       bigint PRIMARY KEY CHECK (seq >= 1),          -- gapless: assigned under the head row's lock, so a missing seq is evidence
    ts        timestamptz NOT NULL,
    actor     text NOT NULL CHECK (length(actor) BETWEEN 1 AND 120),
    action    text NOT NULL CHECK (length(action) BETWEEN 1 AND 60),
    subject   text NOT NULL DEFAULT '' CHECK (length(subject) <= 120),
    payload   jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    prev_hash text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash      text NOT NULL UNIQUE CHECK (hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX audit_ledger_subject_idx ON audit_ledger (subject, seq DESC);
CREATE INDEX audit_ledger_action_idx ON audit_ledger (action, seq DESC);

-- The head: where the chain ends. Every writer updates this one row first, so concurrent payments queue here (briefly: the append
-- is the last statement before COMMIT) and cannot fork the chain. It also lets verify notice entries cut off at the end.
CREATE TABLE audit_ledger_head (
    id   int PRIMARY KEY CHECK (id = 1),
    seq  bigint NOT NULL,
    hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$')
);
INSERT INTO audit_ledger_head (id, seq, hash) VALUES (1, 0, repeat('0', 64));

-- ---- the one definition of the hash (the writer and the verifier both call it) ------------------------------------
CREATE FUNCTION audit_record_hash(p_prev text, p_seq bigint, p_ts timestamptz, p_actor text, p_action text, p_subject text,
                                  p_payload jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(p_prev || jsonb_build_array(
        p_seq, to_char(p_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), p_actor, p_action, p_subject, p_payload)::text,
        'UTF8')), 'hex')
$$;

-- ---- the writer ----------------------------------------------------------------------------------------------------
CREATE FUNCTION audit_append(p_actor text, p_action text, p_subject text DEFAULT '', p_payload jsonb DEFAULT '{}') RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_seq  bigint;
    v_prev text;
    v_ts   timestamptz := clock_timestamp();
    v_hash text;
BEGIN
    -- the row lock IS the serialisation: a second writer waits here until the first commits or rolls back (no fork, no gap)
    UPDATE audit_ledger_head SET seq = seq + 1 WHERE id = 1 RETURNING seq, hash INTO v_seq, v_prev;
    v_hash := audit_record_hash(v_prev, v_seq, v_ts, p_actor, p_action, COALESCE(p_subject, ''), COALESCE(p_payload, '{}'));
    INSERT INTO audit_ledger (seq, ts, actor, action, subject, payload, prev_hash, hash)
    VALUES (v_seq, v_ts, p_actor, p_action, COALESCE(p_subject, ''), COALESCE(p_payload, '{}'), v_prev, v_hash);
    UPDATE audit_ledger_head SET hash = v_hash WHERE id = 1;
    RETURN v_seq;
END
$$;

-- ---- the verifier --------------------------------------------------------------------------------------------------
-- STABLE: it reads one snapshot, so writers running meanwhile cannot make a healthy chain look broken.
CREATE FUNCTION audit_verify_detail() RETURNS TABLE (entries bigint, first_broken bigint, reason text, head_seq bigint, head_hash text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r        audit_ledger%ROWTYPE;
    h        audit_ledger_head%ROWTYPE;
    v_prev   text := repeat('0', 64);
    v_seq    bigint := 0;
    v_broken bigint;
    v_reason text;
BEGIN
    FOR r IN SELECT * FROM audit_ledger ORDER BY seq LOOP
        IF r.seq <> v_seq + 1 THEN
            v_broken := r.seq;
            v_reason := format('entries %s to %s are missing (the sequence must have no gap)', v_seq + 1, r.seq - 1);
        ELSIF r.prev_hash <> v_prev THEN
            v_broken := r.seq;
            v_reason := 'prev_hash does not match the hash of the entry before it (the chain was cut or an entry was inserted)';
        ELSIF r.hash <> audit_record_hash(r.prev_hash, r.seq, r.ts, r.actor, r.action, r.subject, r.payload) THEN
            v_broken := r.seq;
            v_reason := 'the entry''s content does not match its hash (it was changed after it was written)';
        END IF;
        EXIT WHEN v_broken IS NOT NULL;
        v_prev := r.hash;
        v_seq := r.seq;
    END LOOP;
    SELECT * INTO h FROM audit_ledger_head WHERE id = 1;
    IF v_broken IS NULL AND (h.id IS NULL OR h.seq <> v_seq OR h.hash <> v_prev) THEN
        v_broken := v_seq + 1;
        v_reason := format('the ledger ends at entry %s but its head says entry %s (entries were removed from the end, or the head was changed)',
                           v_seq, COALESCE(h.seq, 0));
    END IF;
    RETURN QUERY SELECT (SELECT count(*) FROM audit_ledger), v_broken, v_reason, h.seq, h.hash;
END
$$;

-- The question in one line: the first broken seq, or NULL when the chain is intact.
CREATE FUNCTION audit_verify() RETURNS bigint LANGUAGE sql STABLE AS $$ SELECT first_broken FROM audit_verify_detail() $$;

-- ---- what may never be written (defence in depth for the payload: the app already masks) --------------------------
CREATE FUNCTION audit_ledger_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    txt text := NEW.payload::text;
BEGIN
    IF txt ~* '"(pan|card_number|number|cvc|cvv|cvv2|password|passwd|secret|api_key|authorization|key_hash|webhook_secret|webhook_secret_enc)"\s*:' THEN
        RAISE EXCEPTION 'audit payload has a field that may hold card data or a secret' USING ERRCODE = '22023';
    END IF;
    IF txt ~ '"[0-9]{13,19}"' OR txt ~ '[:\[,]\s*[0-9]{13,19}\s*[,}\]]' THEN
        RAISE EXCEPTION 'audit payload has a value that looks like a card number' USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER audit_ledger_guard BEFORE INSERT ON audit_ledger FOR EACH ROW EXECUTE FUNCTION audit_ledger_guard();

-- ---- append-only ---------------------------------------------------------------------------------------------------
CREATE FUNCTION audit_ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: the audit ledger is append-only', TG_OP, TG_TABLE_NAME USING ERRCODE = '42501';
END
$$;
CREATE TRIGGER audit_ledger_no_change BEFORE UPDATE OR DELETE ON audit_ledger FOR EACH ROW EXECUTE FUNCTION audit_ledger_immutable();
CREATE TRIGGER audit_ledger_no_truncate BEFORE TRUNCATE ON audit_ledger FOR EACH STATEMENT EXECUTE FUNCTION audit_ledger_immutable();
CREATE TRIGGER audit_head_no_delete BEFORE DELETE ON audit_ledger_head FOR EACH ROW EXECUTE FUNCTION audit_ledger_immutable();
CREATE TRIGGER audit_head_no_truncate BEFORE TRUNCATE ON audit_ledger_head FOR EACH STATEMENT EXECUTE FUNCTION audit_ledger_immutable();

-- ---- ... and by privileges too (a second, independent layer) ---------------------------------------------------------
REVOKE ALL ON audit_ledger, audit_ledger_head FROM PUBLIC;
DO $$
BEGIN
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON audit_ledger FROM %I', current_user);
    EXECUTE format('REVOKE DELETE, TRUNCATE ON audit_ledger_head FROM %I', current_user);
END
$$;
