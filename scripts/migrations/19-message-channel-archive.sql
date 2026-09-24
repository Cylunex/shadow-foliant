CREATE TABLE IF NOT EXISTS notification_messages (
    message_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_run_id TEXT,
    category TEXT NOT NULL,
    title_cipher TEXT NOT NULL,
    original_cipher TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    business_as_of TEXT,
    idempotency_key TEXT UNIQUE,
    sensitivity TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_messages_generated
    ON notification_messages(generated_at DESC,message_id DESC);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES notification_messages(message_id),
    channel TEXT NOT NULL,
    target_label TEXT NOT NULL,
    final_body_cipher TEXT NOT NULL,
    final_sha256 TEXT NOT NULL,
    planned_at TEXT,
    attempted_at TEXT,
    acknowledged_at TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    provider_code TEXT,
    error_code TEXT,
    retry_of TEXT,
    fallback_from TEXT,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    last_suppressed_at TEXT,
    suppression_reason TEXT,
    version TEXT NOT NULL,
    UNIQUE(message_id,channel)
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_attempted
    ON notification_deliveries(attempted_at DESC,channel,status);

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('19-message-channel-archive',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
