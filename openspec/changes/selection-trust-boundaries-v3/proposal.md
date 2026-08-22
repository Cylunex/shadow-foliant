# Selection Trust Boundaries V3

## Motivation

The local PIT selector is now the production primary path, but several adjacent
boundaries can still make a run look reproducible while consuming stale or
non-deterministic inputs. Valuation freshness, calendar consensus, historical
PIT coverage, formal artifact shape and research-data readiness need executable
fail-closed contracts.

## Scope

- Require same-day valuation coverage for formal selection.
- Confirm the latest completed trading day with two independent providers.
- Expose and enforce the earliest date for which universe and finance PIT facts
  were actually observable by Foliant.
- Keep immutable selection artifacts free of quotes, holdings and AI review.
- Add protected research-data readiness separate from process readiness.
- Align breadth samples and pairwise returns to their correct universes/dates.
- Remove remaining provenance and legacy-startup ambiguity.

## Compatibility

Existing display and notification consumers keep a merged display overlay, but
the formal `deterministic_top5` and `local_primary_top15` payloads become strict
local artifacts. Runs without calendar consensus or current valuation coverage
return `incomplete` instead of silently degrading.

Confirmed announcement extraction is intentionally outside this change. Until
a deterministic body parser exists, title-only announcements remain excluded
from formal scoring.
