# Implementation and verification

- [x] Add bounded, resumable legacy capture and migration 13 observation index.
- [x] Include archive publication in selection fences; preserve old manifests.
- [x] Add replay history diagnostics and frozen date/adjustment filtering.
- [x] Correct readiness time boundaries and warm both evening cache contexts.
- [x] Add Wencai HTTP cooldown and single-flight admission regressions.
- [x] Preflight isolated evaluation without opening sealed fact contents.
- [x] Run targeted regression tests (120 passed).
- [x] Run full regression suite (544 passed, 2 skipped, 7 subtests) and native
  PostgreSQL real-history acceptance (300 rows; bounded resume, no-op retry,
  replay roundtrip, unchanged business values; isolated schema removed).
- [ ] Apply history capture to production after an explicitly requested deployment.
