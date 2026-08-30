-- Incremental production migration for operational-integrity-v5.
-- Keep this file idempotent: existing installations may already contain a
-- subset of the schema because init_postgres.sql is also used for bootstrap.

ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS broker_execution_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS account_ref TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS import_batch_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS external_fingerprint TEXT;
ALTER TABLE trade_records
    ADD COLUMN IF NOT EXISTS position_effect TEXT NOT NULL DEFAULT 'legacy_unverified';
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS created_by_shadow_user_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_execution
    ON trade_records(source, account_ref, broker_execution_id)
    WHERE broker_execution_id IS NOT NULL AND broker_execution_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_fingerprint
    ON trade_records(external_fingerprint)
    WHERE external_fingerprint IS NOT NULL AND external_fingerprint <> '';

CREATE TABLE IF NOT EXISTS trade_import_batches (
    batch_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    update_position BOOLEAN NOT NULL,
    preview_hash TEXT NOT NULL,
    position_watermark TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    abandoned_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_trade_import_batches_actor
    ON trade_import_batches(actor_id, created_at DESC);
CREATE TABLE IF NOT EXISTS trade_import_rows (
    batch_id TEXT NOT NULL REFERENCES trade_import_batches(batch_id),
    row_number INTEGER NOT NULL,
    external_fingerprint TEXT NOT NULL,
    normalized_payload JSONB NOT NULL,
    validation_status TEXT NOT NULL,
    error_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    trade_record_id BIGINT REFERENCES trade_records(id),
    PRIMARY KEY(batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS research_source_runtime_state (
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error_category TEXT,
    cooldown_until TEXT,
    freshness_as_of TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, endpoint)
);
CREATE TABLE IF NOT EXISTS research_dataset_publications (
    capability TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    dataset_id TEXT NOT NULL,
    effective_as_of TEXT,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_dataset_publication_history (
    capability TEXT NOT NULL,
    generation INTEGER NOT NULL,
    dataset_id TEXT NOT NULL,
    effective_as_of TEXT,
    published_at TEXT NOT NULL,
    PRIMARY KEY(capability, generation)
);

ALTER TABLE selection_input_manifests
    ADD COLUMN IF NOT EXISTS publication_generations TEXT;
CREATE TABLE IF NOT EXISTS research_artifacts (
    artifact_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    run_id TEXT NOT NULL,
    formal INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, artifact_kind)
);
CREATE INDEX IF NOT EXISTS idx_research_artifacts_subject
    ON research_artifacts(subject, created_at DESC);
CREATE TABLE IF NOT EXISTS research_artifact_annotations (
    annotation_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    annotation_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TEXT NOT NULL
);

ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS fencing_token TEXT;
CREATE TABLE IF NOT EXISTS foliant_run_attempts (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    fencing_token TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    PRIMARY KEY(run_id, attempt),
    UNIQUE(fencing_token)
);
CREATE TABLE IF NOT EXISTS foliant_run_progress (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    phase TEXT NOT NULL,
    current_value INTEGER,
    total_value INTEGER,
    message_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_foliant_run_progress_run
    ON foliant_run_progress(run_id, created_at);

INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('09-operational-integrity-v5', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
