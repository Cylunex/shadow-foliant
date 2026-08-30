# Research Trust and Action V7

## Motivation

Foliant already separates formal local selection from Wencai, Miaoxiang and LLM
references, but three trust gaps remain:

- default factor and genome research still use fixed current-stock samples;
- research features, production scoring and strategy deployment gates can drift;
- a confirmed trade cannot optionally retain the formal nomination or decision that
  motivated it, while several subsystems can still emit competing action words.

The production PostgreSQL runtime also retains a legacy notification path that logs
full financial messages and delivery addresses.

## Scope

- ingest listed, delisted and paused security lifecycle rows from zzshare;
- expose a lifecycle-reconstructed research universe with explicit provenance;
- use that universe for default factor and genome research when available;
- centralize production selection feature metadata and consume it in scoring;
- strengthen automatic genome deployment evidence thresholds;
- accept and validate optional selection/strategy/decision origins on trade imports;
- add one deterministic action resolver and use it in the consolidated EOD review;
- remove sensitive notification payloads and destinations from logs;
- update the authoritative architecture and A-share research documentation.

## Compatibility

- Existing selection weights and lane quotas remain unchanged.
- Low-price bull remains an independent formal strategy and keeps the strict
  `price < 20` boundary.
- Trade-origin fields are optional; broker imports without them behave unchanged.
- If lifecycle evidence is unavailable, research falls back to the old representative
  sample but must report an exploratory/survivorship warning.
- No external reference, LLM result or browser state may change formal membership.

