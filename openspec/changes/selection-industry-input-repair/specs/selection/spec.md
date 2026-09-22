# Requirements

## Current universe
The system MUST publish a verified current-listed snapshot independently of unavailable delisted
history and MUST NOT use such a snapshot for retrospective lifecycle reconstruction.

Scenario: the provider confirms L rows but no D rows. Current selection can use a fresh, validated
current snapshot; lifecycle publication and retrospective research remain incomplete.

## Frozen industries
Industry evidence MUST be observed before ingestion and effective before its decision time. All
classified symbols MUST be processed without a portfolio display limit. Historical manifests MUST
continue to use their recorded industry snapshot.

Scenario: a file has 5,000 classified symbols, and is replaced later. New ingestion stores all
eligible classifications; old manifest ranking does not change after the replacement.

## Honest readiness
New live selection MUST reject insufficient classification and stale master inputs. Existing
published results with these deficiencies MUST expose degraded quality without changing membership.

Scenario: industry coverage is zero but the TOP15 artifact exists. The API MUST report degradation
and the affected industry comparison, rather than complete data.

## Independent ranking
New policy versions MUST compare valuation within classified industries and enforce sector counts;
weights MUST stay unchanged and missing required fields MUST NOT receive fabricated scores.

## Research before delivery
Scheduled external research MUST be submitted for the current selection before notification.
Unsuccessful research MUST be reported as degraded, never copied from an older decision.
