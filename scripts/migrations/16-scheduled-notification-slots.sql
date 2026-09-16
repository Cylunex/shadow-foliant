CREATE TABLE IF NOT EXISTS external_research_notification_claims (
    idempotency_key TEXT NOT NULL, overlay_id TEXT NOT NULL,
    notification_slot TEXT NOT NULL, actor_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    PRIMARY KEY(idempotency_key,notification_slot)
);

-- Preserve the old one-shot claim in the planned slot in which it happened.
-- Future slots remain independently claimable for the same immutable overlay.
INSERT INTO external_research_notification_claims
    (idempotency_key,overlay_id,notification_slot,actor_id,consumed_at)
SELECT idempotency_key,overlay_id,
       SUBSTRING(notification_consumed_at FROM 1 FOR 10) || 'T' ||
       CASE
         WHEN SUBSTRING(notification_consumed_at FROM 12 FOR 5) < '11:25' THEN '10:15'
         WHEN SUBSTRING(notification_consumed_at FROM 12 FOR 5) < '14:35' THEN '11:25'
         WHEN SUBSTRING(notification_consumed_at FROM 12 FOR 5) < '20:45' THEN '14:35'
         ELSE '20:45'
       END || '+08:00',
       actor_id,notification_consumed_at
FROM external_research_submissions
WHERE notification_status='consumed' AND notification_consumed_at IS NOT NULL
ON CONFLICT(idempotency_key,notification_slot) DO NOTHING;

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('16-scheduled-notification-slots',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
