-- Price labels and execution accounts have distinct semantics. Old results remain
-- explicitly legacy; do not relabel historical MAE as peak-to-trough drawdown.
ALTER TABLE selection_candidate_outcomes ADD COLUMN IF NOT EXISTS mae_pct REAL;
ALTER TABLE selection_candidate_outcomes ADD COLUMN IF NOT EXISTS metric_version TEXT NOT NULL DEFAULT 'legacy-price-v1';
CREATE TABLE IF NOT EXISTS research_model_orders (
    order_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, baseline TEXT NOT NULL,
    symbol TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_model_orders_state ON research_model_orders(state,created_at);
CREATE TABLE IF NOT EXISTS research_model_portfolios (
    baseline TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_model_targets (
    target_id TEXT PRIMARY KEY, baseline TEXT NOT NULL, run_id TEXT NOT NULL,
    state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_model_targets_pending ON research_model_targets(baseline,state,created_at);
CREATE TABLE IF NOT EXISTS research_experiments (
    trial_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_experiments_fingerprint ON research_experiments(fingerprint);
CREATE TABLE IF NOT EXISTS research_holdout_batches (
    batch_id TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    state TEXT NOT NULL, consumed_by TEXT, consumed_at TEXT
);
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('11-research-decision-loop', NOW()::TEXT) ON CONFLICT(version) DO NOTHING;
