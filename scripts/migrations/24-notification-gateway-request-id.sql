ALTER TABLE notification_deliveries
    ADD COLUMN IF NOT EXISTS gateway_request_id TEXT;

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('24-notification-gateway-request-id',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
