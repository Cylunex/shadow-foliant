"""Shared, bounded valuation readiness; never relabel an older snapshot as current."""

from __future__ import annotations

import os

import pandas as pd


def valuation_lag_budget() -> int:
    """Live policy allows at most one completed trading day, configurable to zero."""
    try:
        return max(0, min(1, int(os.getenv("LOCAL_SELECTION_MAX_VALUATION_LAG", "1"))))
    except (TypeError, ValueError):
        return 0  # Invalid configuration must not relax a gate.


def resolve_valuation(store, market_date: str, symbols, *, min_coverage: float,
                      max_lag: int = 0):
    """Return one dated, quality-checked snapshot and aggregate selection readiness.

    Missing current rows may use the immediately preceding confirmed trading day.
    A partial current snapshot is never mixed with older rows; a complete previous
    snapshot may replace it as a whole. Missing/mismatched dates,
    bad quality, insufficient coverage and calendar uncertainty still fail closed.
    This is selection usability, NOT an exact-date ingestion completion marker.
    """
    latest = store.latest_valuation_as_of(market_date)
    frame = store.load_valuations(latest, exact=True) if latest else pd.DataFrame()
    required = {"symbol", "trade_date", "provider_effective_as_of", "quality_status"}
    def eligible(candidate, day):
        if not required.issubset(candidate.columns):
            return pd.DataFrame()
        return candidate.loc[
            candidate["trade_date"].astype(str).eq(day)
            & candidate["provider_effective_as_of"].astype(str).eq(day)
            & candidate["quality_status"].eq("ok")
        ].drop_duplicates("symbol").copy()

    frame = eligible(frame, latest)
    wanted = set(str(symbol) for symbol in symbols)
    available = set(frame.get("symbol", pd.Series(dtype=str)).astype(str))
    coverage = len(wanted & available) / len(wanted) if wanted else 0.0
    if latest == market_date and coverage < min_coverage and int(max_lag) > 0:
        calendar = store.calendar_consensus(market_date, inclusive=True)
        days = sorted(set(store.trade_days_through(market_date)))
        if (calendar.get("ready") and calendar.get("latest_confirmed_open_date") == market_date
                and len(days) >= 2 and days[-1] == market_date):
            previous = days[-2]
            previous_frame = eligible(store.load_valuations(previous, exact=True), previous)
            previous_symbols = set(previous_frame.get("symbol", pd.Series(dtype=str)).astype(str))
            previous_coverage = len(wanted & previous_symbols) / len(wanted) if wanted else 0.0
            if previous_coverage >= min_coverage:
                latest, frame, coverage = previous, previous_frame, previous_coverage
    lag = store.stale_trading_days(latest, market_date) if latest else None
    fresh = bool(latest and latest == market_date)
    fallback_allowed = False
    if latest and latest < market_date and int(max_lag) > 0 and lag == 1:
        calendar = store.calendar_consensus(market_date, inclusive=True)
        days = sorted(set(store.trade_days_through(market_date)))
        fallback_allowed = bool(
            calendar.get("ready")
            and calendar.get("latest_confirmed_open_date") == market_date
            and len(days) >= 2 and days[-2:] == [latest, market_date]
        )
    ready = bool((fresh or fallback_allowed) and coverage >= min_coverage and wanted)
    state = {
        "ready": ready,
        "status": "current" if ready and fresh else ("lagged" if ready else "unavailable"),
        "valuation_as_of": latest,
        "valuation_coverage": round(coverage, 6),
        "valuation_stale_trading_days": lag,
        "valuation_rows": len(frame),
        "valuation_fresh": fresh,
    }
    return frame, state
