-- Independent, idempotent production migration for execution attribution.
-- No environment-specific values belong in this file.
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS broker_execution_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS account_ref TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS import_batch_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS external_fingerprint TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS position_effect TEXT NOT NULL DEFAULT 'legacy_unverified';
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS created_by_shadow_user_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS selection_run_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS nomination_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS strategy_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS decision_signal_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_trade_records_selection_origin
    ON trade_records(selection_run_id, nomination_id, trade_time DESC)
    WHERE selection_run_id IS NOT NULL OR nomination_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_execution
    ON trade_records(source, account_ref, broker_execution_id)
    WHERE broker_execution_id IS NOT NULL AND broker_execution_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_fingerprint
    ON trade_records(external_fingerprint)
    WHERE external_fingerprint IS NOT NULL AND external_fingerprint <> '';

INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('10-trade-attribution-action-contract', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
