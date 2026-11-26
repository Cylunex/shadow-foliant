ALTER TABLE IF EXISTS daily_pnl_snapshots
    ADD COLUMN IF NOT EXISTS fund_updated_at TEXT,
    ADD COLUMN IF NOT EXISTS fund_prior_fresh_count INTEGER,
    ADD COLUMN IF NOT EXISTS fund_revision_delta DOUBLE PRECISION;

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('25-fund-pnl-revision',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
