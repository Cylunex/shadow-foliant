# Proposal: add Fuyao AICubes official market-data provider

## Motivation

Foliant needs an authenticated official source for A-share calendar, batch quotes, daily bars and
fundamentals while retaining its existing independent fallbacks and point-in-time safeguards.

## Scope

- Add one atomic `fuyao_aicubes` REST provider.
- Route calendar, batch quote, daily history, valuation, financial, auction and special-data abilities.
- Preserve old public datahub signatures and fallbacks.
- Explicitly degrade the documented unavailable external capital-flow ability.
- Add secret-safe configuration, observability, tests and a read-only smoke probe.

## Compatibility

The provider is inactive without an external key. Existing routes therefore behave unchanged on
unconfigured installations. No schema migration or raw response retention is introduced.
