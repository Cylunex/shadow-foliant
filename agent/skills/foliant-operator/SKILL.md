---
name: foliant-operator
description: Read and review purpose-scoped Foliant research packets through a JSON CLI.
---
Use scripts/foliant --config /path/to/private/config.json catalog or call NAME.
Calls consume JSON on stdin. Configuration fixes worker, owner and allowed purposes.
Claim returns frozen candidates and evidence. Review at most three, cite evidence_id,
give strongest counterevidence and next_check. Return an empty reviews array when
evidence is insufficient. Directed reviews are not independent discoveries.
Use original object_id and revision to submit. After uncertain writes, status first.
No trading, policy publishing, notification sending or arbitrary database queries.
