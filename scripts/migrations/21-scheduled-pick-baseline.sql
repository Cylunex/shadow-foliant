ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS pick_fingerprint TEXT;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS pick_origin_fingerprint TEXT;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS pick_selection_run_id TEXT;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS pick_research_id TEXT;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS pick_sources TEXT;
ALTER TABLE scheduled_notification_deliveries
    ADD COLUMN IF NOT EXISTS pick_included INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS scheduled_notification_silent_slots (
    notification_slot TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL, reason TEXT NOT NULL
);

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('21-scheduled-pick-baseline',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
