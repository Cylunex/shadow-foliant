# Operational Integrity V5

## Motivation

Foliant already has point-in-time formal selection artifacts and lease-based Agent preview Runs,
but adjacent operational paths do not yet provide the same integrity guarantees. Trade import can
silently clamp an oversized sale while recording the full fill, scheduled thread tasks can continue
writing after timeout, source contracts expose only static limits, and a formal selector can span
two independently published dataset versions.

## Scope

- Make stock trade preview and confirmation position-aware, atomic and execution-idempotent.
- Reject no-position and oversized sales whenever a trade changes the current position.
- Extend the existing Run lease model with attempts, fencing tokens and progress events that can be
  reused by scheduled jobs.
- Reject result publication by expired or superseded workers.
- Combine static source contracts with safe runtime availability, quota, cooldown and freshness.
- Route providers by dataset while preserving zzshare primary, BaoStock qfq repair, TDX raw
  validation/quotes and Wencai reference-only boundaries.
- Bind formal reads to a stable publication-generation vector.
- Add an append-only `research-artifact-v1` contract with evidence, freshness, invalidation and
  provenance; AI annotations remain non-authoritative overlays.
- Split oversized runtime and API modules only after their behavior is covered by contracts.

## Compatibility

- Existing Web and Agent URLs remain available; additive fields and focused read endpoints are used.
- Existing trade rows are retained and marked legacy when they lack an execution identity.
- Existing formal selection artifacts remain authoritative and replayable.
- PostgreSQL remains the only production business database. SQLite remains test-injected only.
- Wencai remains a separately persisted reference and never supplies formal candidates or scores.
- No production hosts, domains, credentials, financial holdings or trade details enter repository
  configuration, documentation, telemetry or logs.

