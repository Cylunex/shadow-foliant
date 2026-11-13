---
name: foliant-operator
description: Read and review purpose-scoped Foliant research packets through a JSON CLI.
---
Use scripts/foliant --config /path/to/private/config.json catalog or call NAME.
Calls consume JSON on stdin. Configuration fixes worker, owner and allowed purposes.
Pass a stable `claim_id` to `claim` for each run. Retrying with the same owner, worker
and key recovers the original packet and its current `claimed`, `received` or
`expired` state. If another claim took its lease, the CLI returns
`research_generation_conflict` with the original `object_id`. An empty queue
returns `{"status":"empty"}`. Omitting the key retains the legacy polling behavior.

An active claim returns frozen candidates and evidence. Review at most three, cite evidence_id,
give strongest counterevidence and next_check. Return an empty reviews array when
evidence is insufficient. Directed reviews are not independent discoveries.
For `submit`, pass the original `object_id` as `identity`, the claim revision as
`generation`, and the review object as `result`. A repeat of the accepted submission returns its receipt.
Domain rejections exit with code 1 and emit `{"isError":true,"code":"..."}` on stdout.
`research_deadline_expired` identifies a late submission, `research_generation_conflict`
a stale generation, and `research_result_conflict` a change to an accepted result.
`research_result_invalid`, `research_budget_invalid`, `research_fields_invalid`,
`research_subject_invalid`, `research_evidence_invalid`, `research_text_invalid` and
`research_record_too_large` identify rejected inputs. For `outcome_unknown` or a lost
response, recover a claim by its key or query `status` with the original `identity`
before retrying a submission.
No trading, policy publishing, notification sending or arbitrary database queries.
