# Proposal: local point-in-time A-share selection

## Motivation

The existing morning selector treats Wencai, Miaoxiang, InStock and an on-demand
factor scan as interchangeable candidate generators. Missing fields are often
neutral, identical discovery ideas can be double-counted, and a remote natural-
language service can therefore dominate the final ranking. Long-running bulk
requests also collide with the trading-time workload.

## Change

- Make a local, versioned point-in-time warehouse the only primary selection input.
- Use zzshare for whole-market daily, valuation and PIT financial snapshots, with
  bounded BaoStock qfq repair for exact-date adjusted-market-data gaps. TDX raw
  bars remain an independent validation/quote source.
- Formalize provider capability, authentication, pagination, concurrency, timeout,
  retry, frequency and PIT semantics as executable contracts.
- Rank fundamentals/valuation to 200, 60-trading-day structure to 50, apply
  120/250-day risk corrections, diversify by industry, and retain 5-15 final names.
- Keep Wencai running temporarily as a separately persisted reference set. It MUST
  NOT affect gates, scores, ranking or incomplete-data fallback.
- Persist structured official events and apply asymmetric, type-specific risk decay.

## Compatibility

The existing `unified_selection` task name, TOP15 delivery artifact, deterministic
TOP5 projection and later Agent review remain. The meaning of its candidate score
changes from source-hit count to the documented local score. Until the warehouse
has sufficient coverage, the task fails closed instead of silently restoring the
old Wencai-led behavior.
