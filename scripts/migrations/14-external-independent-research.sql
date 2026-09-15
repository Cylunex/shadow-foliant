CREATE TABLE IF NOT EXISTS external_research_evidence (
    evidence_id TEXT PRIMARY KEY, channel TEXT NOT NULL,
    source_url TEXT NOT NULL, source_type TEXT NOT NULL,
    published_at TEXT NOT NULL, event_at TEXT, captured_at TEXT NOT NULL,
    decision_as_of TEXT NOT NULL, symbols JSONB NOT NULL, industries JSONB NOT NULL,
    direction DOUBLE PRECISION NOT NULL, confidence DOUBLE PRECISION NOT NULL,
    expiry TEXT NOT NULL, dedupe_key TEXT NOT NULL,
    primary_source_confirmed INTEGER NOT NULL, controversy_status TEXT NOT NULL,
    payload_hash TEXT NOT NULL, payload JSONB NOT NULL,
    actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(channel,dedupe_key)
);
CREATE TABLE IF NOT EXISTS external_independent_overlays (
    overlay_id TEXT PRIMARY KEY, channel TEXT NOT NULL,
    selection_run_id TEXT NOT NULL, base_strategy_version TEXT NOT NULL,
    decision_as_of TEXT NOT NULL, ranking_locked_at TEXT NOT NULL,
    market_regime TEXT NOT NULL, payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_news_watchlist (
    watch_id TEXT PRIMARY KEY, channel TEXT NOT NULL,
    selection_run_id TEXT NOT NULL, decision_as_of TEXT NOT NULL,
    symbol TEXT NOT NULL, payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(channel,selection_run_id,decision_as_of,symbol)
);
CREATE TABLE IF NOT EXISTS external_overlay_outcomes (
    overlay_id TEXT NOT NULL, symbol TEXT NOT NULL,
    horizon_days INTEGER NOT NULL, decision_as_of TEXT NOT NULL,
    market_regime TEXT NOT NULL, base_score DOUBLE PRECISION NOT NULL,
    event_adjustment DOUBLE PRECISION NOT NULL, risk_veto INTEGER NOT NULL,
    return_pct DOUBLE PRECISION, outcome_status TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY(overlay_id,symbol,horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_external_evidence_decision
    ON external_research_evidence(channel,decision_as_of);
CREATE INDEX IF NOT EXISTS idx_external_overlay_latest
    ON external_independent_overlays(channel,ranking_locked_at);
CREATE INDEX IF NOT EXISTS idx_external_outcomes_horizon
    ON external_overlay_outcomes(horizon_days,decision_as_of);
INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('14-external-independent-research',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
