# Research Reproducibility V4

## Motivation

The forward selector has a local deterministic ranking boundary, but several inputs can still be mislabeled, partially published, revised in place, or interpreted with inconsistent cutoff semantics. Formal TOP5 output is also stored through a best-effort display snapshot instead of an append-only research artifact.

## Scope

- Introduce one explicit decision context for universe, market, valuation, financial, and event reads.
- Stage and validate security-master snapshots before publishing them.
- Preserve provider observations and bind formal runs to immutable input manifests and versioned policies.
- Require provider-effective valuation dates and aligned financial statement periods.
- Make formal TOP15/TOP5 artifacts append-only and authoritative.
- Fail closed on unknown net assets, policy relaxation, and insufficient correlation data.
- Align factor evaluation and benchmark comparisons by real trading dates.
- Separate data readiness from selection readiness and strengthen deployment verification.
- Add resumable bootstrap checkpoints and PostgreSQL-native integration coverage.

## Compatibility

- Existing canonical research tables remain readable while append-only observation and artifact tables are introduced.
- Existing Web response keys may remain as display aliases, but formal consumers MUST read the authoritative artifact.
- Strict historical replay remains unavailable before the recorded PIT boundary.
- Repository examples remain fictional and contain no production infrastructure.

