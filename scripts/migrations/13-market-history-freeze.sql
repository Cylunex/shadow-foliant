CREATE INDEX IF NOT EXISTS idx_market_observation_identity
ON research_market_observations(dataset_id,symbol,trade_date,adjustment);
INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('13-market-history-freeze',NOW()::TEXT) ON CONFLICT(version) DO NOTHING;
