# Research Artifact Requirements

## Requirement: research claims cite immutable evidence

Every formal research artifact MUST contain versioned evidence references, cutoff/freshness metadata,
quality, invalidation conditions and provenance.

### Scenario: preferred data is missing

- **WHEN** a research result lacks a preferred input
- **THEN** the artifact records the missing evidence and reduced confidence instead of inventing or
  neutralizing the fact

## Requirement: AI text is non-authoritative

LLM annotations MUST NOT alter formal evidence, selection membership, score or order.

### Scenario: AI disagrees with formal ranking

- **WHEN** an annotation recommends a different order
- **THEN** the formal artifact remains unchanged and the annotation is stored separately

