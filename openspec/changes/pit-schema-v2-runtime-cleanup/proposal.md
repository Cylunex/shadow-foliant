# Proposal: PIT schema V2 and runtime cleanup

## Motivation

The first local-selection release still overwrote security-master history and
financial-report revisions, accepted stale snapshots, treated title-only
announcements as actionable alerts, and retained several silent SQLite fallback
paths. Those behaviors make historical selection irreproducible and can split
production state across databases.

## Change

- Store dated security-master snapshots and immutable financial report revisions.
- Require the expected prior completed trading day, normalized qfq units, market
  coverage, and minimum financial coverage before producing candidates.
- Make the formal TOP5 a deterministic projection of the local PIT TOP15; attach
  Agent, quote and Wencai results as review/reference metadata only.
- Admit only body-confirmed, structured disclosures to the formal event store.
- Make PostgreSQL the sole production persistence backend and remove Streamlit,
  Docker and scheduled SQLite-backup artifacts.
- Add repository CI for compile, shell syntax and unit tests.

## Compatibility

Existing task and API names remain. V1 warehouse tables are retained for one
rollback window and copied idempotently into V2; no production financial or
portfolio data is deleted. Runs with insufficient V2 data return `incomplete`
instead of falling back to external discovery.
