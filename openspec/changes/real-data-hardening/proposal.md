# Real-data acceptance hardening

## Motivation

Real acceptance found longer mutable bar history than immutable manifest history,
premature postclose readiness failures, repeated Wencai HTTP refusals, and a
missing isolated worker that could consume a sealed evaluation reservation.

## Scope and compatibility

- Add explicit bounded legacy-history capture for future manifests only.
- Diagnose frozen replay coverage and enforce date/adjustment boundaries.
- Separate morning selection readiness from scheduled evening acquisition.
- Short-circuit deterministic source refusals and enforce one in-flight Wencai query.
- Validate isolated worker infrastructure with public synthetic data before reservation.

No change to formal ranking, reference-only source boundaries, real account state,
historical artifacts, provider quotas, or notification content. No automatic deployment
or production history migration accompanies this change.
