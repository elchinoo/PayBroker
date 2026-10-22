-- Training data capture, labels and the model registry. Every statement is idempotent so the file is safe to re-run and to
-- apply on top of a database that already holds payments (the new columns are nullable; nothing is rewritten).

-- The kind of chargeback matters for labelling: a fraud-coded dispute is a fraud label, a "product not received" is not.
ALTER TABLE chargeback ADD COLUMN IF NOT EXISTS reason_code text;

-- Which model produced the score (null for payments scored before this migration).
ALTER TABLE fraud_assessment ADD COLUMN IF NOT EXISTS model_version text;

-- One row per scored payment, written only while capture is switched on (off by default). No card, e-mail, IP, device id or
-- name is ever stored here: the payment id is an internal reference and the 17 numbers are derived features.
CREATE TABLE IF NOT EXISTS training_example (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    payment_id    text NOT NULL,
    features      real[] NOT NULL CHECK (cardinality(features) BETWEEN 1 AND 256),
    feature_hash  text NOT NULL,                       -- identifies the feature list and order the vector was built with
    model_version text NOT NULL,
    score         int NOT NULL CHECK (score BETWEEN 0 AND 1000),
    decision      text NOT NULL CHECK (decision IN ('APPROVE', 'CHALLENGE', 'DECLINE')),
    label         smallint CHECK (label IN (0, 1)),    -- 1 fraud, 0 legitimate; null until a real outcome is known
    label_source  text CHECK (label_source IN ('chargeback', 'case', 'aging')),
    labeled_at    timestamptz,
    CHECK ((label IS NULL) = (label_source IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS training_example_payment_idx ON training_example (payment_id);
CREATE INDEX IF NOT EXISTS training_example_created_idx ON training_example (created_at);

-- Uploaded models (JSON trees). At most one row is active; with none active the bundled file is used.
CREATE TABLE IF NOT EXISTS model_version (
    id          bigserial PRIMARY KEY,
    version     text NOT NULL UNIQUE CHECK (version ~ '^[A-Za-z0-9._-]{1,64}$'),
    notes       text NOT NULL DEFAULT '',
    uploaded_by text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    size_bytes  int NOT NULL,
    tree_count  int NOT NULL,
    sha256      text NOT NULL,
    active      boolean NOT NULL DEFAULT false,
    doc         jsonb NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS model_version_one_active ON model_version ((true)) WHERE active;
