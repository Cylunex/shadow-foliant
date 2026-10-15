-- Fund v2 facts retain exact Decimal text inside JSON payloads.
-- Legacy fund_holdings stays a compatibility projection, no automatic historical rewrite.
CREATE TABLE IF NOT EXISTS fund_ledger_openings (
    code TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_ledger_events (
    event_id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    code TEXT NOT NULL,
    event_date DATE NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fund_ledger_date ON fund_ledger_events(code,event_date);
CREATE TABLE IF NOT EXISTS fund_workspace (
    kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(kind,item_key)
);

INSERT INTO research_schema_migrations(version,applied_at)
VALUES ('22-fund-research-upgrade',NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
