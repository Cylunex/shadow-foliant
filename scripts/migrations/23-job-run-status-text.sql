-- Older installations used an enum limited to success/error/skipped/running.
-- Match init_postgres.sql so partial/degraded outcomes remain real states.
ALTER TABLE job_runs ALTER COLUMN status TYPE TEXT USING status::TEXT;

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('23-job-run-status-text',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
