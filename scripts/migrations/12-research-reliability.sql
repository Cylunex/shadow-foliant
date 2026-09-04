CREATE TABLE IF NOT EXISTS research_reliability_records (
    kind TEXT NOT NULL, object_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    revision INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(kind,object_id,owner_id,revision)
);
CREATE INDEX IF NOT EXISTS idx_reliability_owner ON research_reliability_records(owner_id,kind,created_at);
CREATE TABLE IF NOT EXISTS research_reliability_work (
    work_id TEXT PRIMARY KEY, kind TEXT NOT NULL, priority INTEGER NOT NULL,
    state TEXT NOT NULL, attempts INTEGER NOT NULL, available_at TEXT NOT NULL,
    payload TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('12-research-reliability',NOW()::TEXT) ON CONFLICT(version) DO NOTHING;
