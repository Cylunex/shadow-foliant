CREATE TABLE IF NOT EXISTS scheduled_notification_deliveries (
    notification_slot TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    original_lines INTEGER NOT NULL,
    delivered_lines INTEGER NOT NULL,
    category TEXT NOT NULL,
    version TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    attempted_at TEXT,
    delivered_at TEXT,
    http_status INTEGER,
    error_code TEXT,
    delivery_status TEXT NOT NULL,
    suppression_reason TEXT,
    suppressed_count INTEGER NOT NULL DEFAULT 0
);

-- Preserve already claimed legacy slots during a mid-slot rollout. A claim
-- without a confirmed receipt stays unknown; it is never promoted to sent.
INSERT INTO scheduled_notification_deliveries
    (notification_slot,actor_id,payload_hash,original_lines,delivered_lines,
     category,version,claimed_at,attempted_at,delivered_at,http_status,
     error_code,delivery_status,suppression_reason)
SELECT notification_slot,MIN(actor_id),repeat('0',64),0,0,
       'report','legacy-external-claim',MIN(consumed_at),
       MAX(delivery_attempted_at),MAX(delivered_at),NULL,
       MAX(delivery_error_code),
       CASE WHEN COUNT(*) FILTER (WHERE delivery_status='delivered')>0 THEN 'delivered'
            WHEN COUNT(*) FILTER (WHERE delivery_status='failed')>0 THEN 'failed'
            ELSE 'unknown' END,
       'legacy_claim_migrated'
FROM external_research_notification_claims
GROUP BY notification_slot
ON CONFLICT(notification_slot) DO NOTHING;

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('18-scheduled-notification-audit',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
