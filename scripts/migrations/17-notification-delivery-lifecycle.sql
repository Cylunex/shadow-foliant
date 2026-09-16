ALTER TABLE external_research_notification_claims
    ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE external_research_notification_claims
    ADD COLUMN IF NOT EXISTS delivery_attempted_at TEXT;
ALTER TABLE external_research_notification_claims
    ADD COLUMN IF NOT EXISTS delivered_at TEXT;
ALTER TABLE external_research_notification_claims
    ADD COLUMN IF NOT EXISTS delivery_error_code TEXT;

-- Existing rows predate delivery acknowledgements.  They must remain unknown:
-- treating a historical claim as delivered could falsely report that QQ succeeded.
UPDATE external_research_notification_claims
SET delivery_status='unknown'
WHERE delivery_status IS NULL OR delivery_status='';

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('17-notification-delivery-lifecycle',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
