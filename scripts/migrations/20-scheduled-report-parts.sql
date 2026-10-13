ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS part_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS accepted_part_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS message_archive_status TEXT;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS message_archive_id TEXT;

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('20-scheduled-report-parts',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
