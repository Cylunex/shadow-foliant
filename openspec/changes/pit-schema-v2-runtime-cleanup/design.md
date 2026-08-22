# Design

## Point-in-time storage

`research_security_snapshots` is keyed by `(snapshot_date, symbol)`.
`research_financial_facts` is keyed by report type, symbol, statement date,
publication date, provider and revision. Queries constrain both publication and
first-seen dates before choosing the newest visible revision. V1 rows are copied
idempotently and remain available only for rollback.

## Completeness boundary

The local trade calendar determines the latest completed trading day strictly
before the selection date. Selection fails closed when the newest usable qfq bar
does not equal that date, when normalized-market coverage is below policy, or
when fewer than the configured fraction of eligible securities have at least four
of six fundamental/valuation metrics. Unknown volume units are unusable.

zzshare remains primary. BaoStock may repair exact-date qfq gaps. Raw TDX data is
validation/quote data and cannot impersonate adjusted history. Endpoint gates are
process-local and use a shared Redis rate slot when available.

## Recommendation boundary

The formal snapshot records four distinct objects:

1. local primary TOP15;
2. deterministic TOP5 derived only from local score/rank/code;
3. Agent review that cannot veto or reorder;
4. Wencai/external reference that cannot add candidates or score.

## Event boundary

Title-only classification is a discovery clue. A formal event requires a stable
document identifier, confirmed body-derived fields and explicit materiality,
surprise, novelty, entity impact and source provenance. Only confirmed events are
persisted or scored.

## Runtime cleanup

PostgreSQL is mandatory for business state and OIDC server sessions. Test suites
may inject an explicit temporary SQLite adapter, but production code has no local
database fallback. Database backups belong to restricted infrastructure using
PostgreSQL-native tooling, not an application job. Supervisor remains the
production process manager; repository Docker and Streamlit launchers are removed.
