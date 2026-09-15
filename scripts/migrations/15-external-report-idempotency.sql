CREATE TABLE IF NOT EXISTS external_research_submissions (
    idempotency_key TEXT PRIMARY KEY, channel TEXT NOT NULL,
    overlay_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
    notification_status TEXT NOT NULL,
    notification_consumed_at TEXT, actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('15-external-report-idempotency',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
