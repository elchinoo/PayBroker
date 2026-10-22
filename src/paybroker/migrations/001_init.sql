-- PayBroker schema. Money is stored in minor units (cents). No PAN, expiry-with-PAN or CVV is ever stored.

-- ---- operator side: LLM accounts --------------------------------------------
-- (settings, browser sessions, operator API tokens and OIDC login state are created by agentpg-kit)

CREATE TABLE llm_account (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 60),
    provider      text NOT NULL DEFAULT 'custom',
    base_url      text NOT NULL,
    api_key_enc   text,
    api_key_last4 text,
    chat_model    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ---- merchants -------------------------------------------------------------------------------------------
CREATE TABLE merchant (
    id                 text PRIMARY KEY CHECK (id ~ '^mch_[a-z0-9_]{4,40}$'),
    name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    webhook_url        text,
    webhook_secret_enc text,                     -- encrypted with the vault key; shown once at creation
    settings           jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merchant_key (
    id           bigserial PRIMARY KEY,
    merchant_id  text NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('secret', 'publishable')),
    name         text NOT NULL DEFAULT '',
    prefix       text NOT NULL,
    key_hash     text NOT NULL UNIQUE,           -- sha256 of the key; the key is shown once
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);

-- ---- vault: card tokens (metadata only) -----------------------------------------------------------------
CREATE TABLE card (
    token         text PRIMARY KEY,
    merchant_id   text NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    brand         text NOT NULL,
    bin           text NOT NULL,
    last4         text NOT NULL,
    exp_month     smallint NOT NULL CHECK (exp_month BETWEEN 1 AND 12),
    exp_year      smallint NOT NULL,
    fingerprint   text NOT NULL,                 -- keyed hash of the number: links the same card across tokens
    sim_behavior  text NOT NULL DEFAULT 'random',  -- how the simulated PSPs treat this TEST card
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX card_fingerprint_idx ON card (fingerprint);

-- ---- payments ---------------------------------------------------------------------------------------------
CREATE TABLE transaction (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id       text NOT NULL UNIQUE,
    merchant_id      text NOT NULL REFERENCES merchant(id),
    idempotency_key  text NOT NULL,
    request_hash     text NOT NULL,
    amount           bigint NOT NULL CHECK (amount > 0),
    currency         text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status           text NOT NULL CHECK (status IN ('PROCESSING', 'REQUIRES_ACTION', 'AUTHORIZED', 'CAPTURED',
                       'PARTIALLY_REFUNDED', 'REFUNDED', 'VOIDED', 'DECLINED', 'FAILED')),
    customer_id      text,
    card_token       text,
    capture_method   text NOT NULL DEFAULT 'manual' CHECK (capture_method IN ('manual', 'automatic')),
    request          jsonb NOT NULL DEFAULT '{}',   -- the sanitised request (no secrets) for audit and re-authorization
    fraud_score      int NOT NULL DEFAULT 0,
    fraud_decision   text NOT NULL DEFAULT 'APPROVE' CHECK (fraud_decision IN ('APPROVE', 'CHALLENGE', 'DECLINE')),
    psp_selected     text,
    processor_tx_id  text,
    auth_code        text,
    decline_code     text,
    amount_captured  bigint NOT NULL DEFAULT 0,
    amount_refunded  bigint NOT NULL DEFAULT 0,
    latency_ms       int,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, idempotency_key)
);
CREATE INDEX tx_merchant_created_idx ON transaction (merchant_id, created_at DESC);
CREATE INDEX tx_created_idx ON transaction (created_at DESC);
CREATE INDEX tx_status_idx ON transaction (status);

CREATE TABLE three_ds_challenge (
    id             text PRIMARY KEY,
    transaction_id uuid NOT NULL REFERENCES transaction(id) ON DELETE CASCADE,
    status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed', 'expired')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL
);

-- ---- fraud engine -------------------------------------------------------------------------------------
CREATE TABLE fraud_assessment (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id         uuid NOT NULL REFERENCES transaction(id) ON DELETE CASCADE,
    tier1_rules_triggered  text[] NOT NULL DEFAULT '{}',
    tier2_ml_score         int NOT NULL,
    tier2_feature_snapshot jsonb NOT NULL,
    reasons                text[] NOT NULL DEFAULT '{}',
    decision               text NOT NULL,
    action_taken           text NOT NULL DEFAULT 'NONE',
    timings                jsonb NOT NULL DEFAULT '{}',
    feature_vector         vector(17),
    tier3_state            text NOT NULL DEFAULT 'none' CHECK (tier3_state IN ('none', 'queued', 'done', 'failed')),
    tier3_llm_analysis     jsonb,                    -- filled asynchronously
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fa_tx_idx ON fraud_assessment (transaction_id);
CREATE INDEX fa_vec_idx ON fraud_assessment USING hnsw (feature_vector vector_cosine_ops);

CREATE TABLE fraud_case (
    id               bigserial PRIMARY KEY,
    transaction_id   uuid NOT NULL UNIQUE REFERENCES transaction(id) ON DELETE CASCADE,
    status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    analyst_decision text CHECK (analyst_decision IN ('PERMANENT_BLOCK', 'ALLOW_WITH_KYC', 'DISMISS_ALERT')),
    note             text,
    resolved_by      text,
    resolved_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fc_status_idx ON fraud_case (status, created_at DESC);

CREATE TABLE fs_entity (            -- the feature store (key fs:{entity_type}:{entity_id})
    entity_type text NOT NULL CHECK (entity_type IN ('user', 'device', 'card', 'ip')),
    entity_id   text NOT NULL,
    features    jsonb NOT NULL DEFAULT '{}',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE blacklist (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('ip', 'device', 'card_fingerprint', 'bin', 'email_domain')),
    value      text NOT NULL CHECK (length(value) BETWEEN 1 AND 200),
    reason     text NOT NULL DEFAULT '',
    source     text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'case', 'chargeback')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, value)
);

CREATE TABLE fraud_rule (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 80),
    description text NOT NULL DEFAULT '',
    definition  jsonb NOT NULL,
    action      text NOT NULL CHECK (action IN ('decline', 'challenge')),
    enabled     boolean NOT NULL DEFAULT true,
    created_by  text NOT NULL DEFAULT 'system',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rule_suggestion (       -- proposed by the LLM; a person approves or rejects, nothing activates itself
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    definition  jsonb NOT NULL,
    action      text NOT NULL CHECK (action IN ('decline', 'challenge')),
    rationale   text NOT NULL DEFAULT '',
    evidence    jsonb NOT NULL DEFAULT '{}',
    source      text NOT NULL DEFAULT 'llm',
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);

CREATE TABLE chargeback (
    id          bigserial PRIMARY KEY,
    payment_id  text NOT NULL REFERENCES transaction(payment_id),
    customer_id text,
    amount      bigint NOT NULL,
    reason      text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- routing -----------------------------------------------------------------------------------------
CREATE TABLE psp_connector (
    name            text PRIMARY KEY CHECK (name ~ '^[a-z0-9_]{2,30}$'),
    label           text NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    currencies      text[] NOT NULL,
    regions         text[] NOT NULL DEFAULT '{}',          -- data-residency regions this connector can serve
    fee_bps         int NOT NULL CHECK (fee_bps BETWEEN 0 AND 1000),
    fixed_fee_cents int NOT NULL DEFAULT 0,
    base_latency_ms int NOT NULL,                          -- simulated connector behaviour
    base_auth_rate  numeric(5, 4) NOT NULL,
    error_rate      numeric(5, 4) NOT NULL DEFAULT 0.01,
    timeout_rate    numeric(5, 4) NOT NULL DEFAULT 0.005,
    auth_ewma       numeric(6, 5) NOT NULL,                -- observed
    latency_ewma    numeric(8, 2) NOT NULL,
    attempts        bigint NOT NULL DEFAULT 0,
    successes       bigint NOT NULL DEFAULT 0
);

-- ---- events: the Kafka replacement ------------------------------------------------------------------------
CREATE TABLE outbox (
    id           bigserial PRIMARY KEY,
    topic        text NOT NULL CHECK (topic IN ('webhook', 'fraud.triage', 'fraud.rules')),
    payload      jsonb NOT NULL,
    available_at timestamptz NOT NULL DEFAULT now(),
    attempts     int NOT NULL DEFAULT 0,
    done_at      timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_ready_idx ON outbox (available_at) WHERE done_at IS NULL;

CREATE TABLE webhook_event (
    event_id     text PRIMARY KEY,
    merchant_id  text NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    type         text NOT NULL,
    body         jsonb NOT NULL,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed', 'skipped')),
    attempts     int NOT NULL DEFAULT 0,
    last_code    int,
    created_at   timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);
CREATE INDEX webhook_event_merchant_idx ON webhook_event (merchant_id, created_at DESC);
