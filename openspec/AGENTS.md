# OpenSpec workflow

This directory records architecture-level changes before or alongside implementation.

For each breaking security, architecture, or cross-project change:

1. Create `changes/<change-id>/proposal.md` describing motivation, scope, and compatibility.
2. Add `design.md` for trust boundaries, data flows, and rejected alternatives.
3. Track implementation and verification in `tasks.md`.
4. Add requirement deltas under `specs/<capability>/spec.md` using MUST/SHOULD language and
   executable scenarios.
5. Keep production hosts, domains, ports, credentials, and private infrastructure out of specs.

The implementation remains authoritative only after its tests pass. A proposal never grants broader
access than the repository and workspace security instructions allow.
