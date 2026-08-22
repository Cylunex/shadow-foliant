"""Protected aggregate readiness for the local research warehouse."""

from __future__ import annotations

from datetime import date
import json
import os
from typing import Any, Dict, Optional

from data.research_store import ResearchStore


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def snapshot(*, store: Optional[ResearchStore] = None,
             selection_date: Optional[str] = None) -> Dict[str, Any]:
    """Return aggregate freshness/coverage only; never return symbols or payloads."""
    store = store or ResearchStore(ensure_schema=False)
    selection_date = str(selection_date or date.today().isoformat())
    pit = store.pit_coverage(selection_date)
    calendar = store.calendar_consensus(selection_date)
    expected = calendar.get("latest_confirmed_open_date")
    universe = store.load_universe(selection_date)
    universe_count = int(len(universe.drop_duplicates("symbol"))) if not universe.empty else 0
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT MAX(trade_date) FROM research_daily_bars
               WHERE adjustment='qfq' AND close>0
               AND quality_status NOT IN ('failed','unknown_unit')"""
        )
        row = cur.fetchone()
        actual = str(row[0]) if row and row[0] else None
        usable_count = store.daily_bar_symbol_count(actual, adjustment="qfq") if actual else 0
        valuation_as_of = store.latest_valuation_as_of(actual or selection_date)
        cur.execute(
            """SELECT COUNT(DISTINCT symbol) FROM research_valuations
               WHERE trade_date=? AND quality_status NOT IN ('failed','unknown_unit')""",
            (valuation_as_of or "",),
        )
        valuation_count = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            """SELECT COUNT(DISTINCT symbol) FROM research_financial_facts
               WHERE pub_date<=? AND first_seen_as_of<=?""",
            (selection_date, selection_date),
        )
        financial_symbol_count = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            """SELECT as_of,status,quality_status FROM research_sync_runs
               WHERE capability='daily_market' ORDER BY started_at DESC LIMIT 1"""
        )
        sync_row = cur.fetchone()
        cur.execute(
            """SELECT selection_date,status,metadata FROM selection_runs
               ORDER BY created_at DESC LIMIT 1"""
        )
        selection_row = cur.fetchone()
    finally:
        conn.close()

    usable_coverage = _ratio(usable_count, universe_count)
    valuation_coverage = _ratio(valuation_count, universe_count)
    financial_coverage = _ratio(financial_symbol_count, universe_count)
    last_selection = None
    if selection_row:
        metadata = json.loads(selection_row[2] or "{}")
        if metadata.get("market_as_of") == expected:
            financial_coverage = float(
                metadata.get("financial_coverage", financial_coverage) or 0.0
            )
        last_selection = {
            "selection_date": str(selection_row[0]),
            "status": str(selection_row[1]),
            "market_as_of": metadata.get("market_as_of"),
        }
    last_sync = ({
        "as_of": str(sync_row[0]), "status": str(sync_row[1]),
        "quality_status": str(sync_row[2]),
    } if sync_row else None)

    minimum_market = float(os.getenv("LOCAL_SELECTION_MIN_WAREHOUSE_COVERAGE", "0.80"))
    minimum_valuation = float(os.getenv("LOCAL_SELECTION_MIN_VALUATION_COVERAGE", "0.70"))
    minimum_finance = float(os.getenv("LOCAL_SELECTION_MIN_FINANCIAL_COVERAGE", "0.70"))
    checks = {
        "calendar_consensus": bool(calendar.get("ready") and expected),
        "market_fresh": bool(expected and actual == expected),
        "market_coverage": usable_coverage >= minimum_market,
        "valuation_fresh": bool(expected and valuation_as_of == expected),
        "valuation_coverage": valuation_coverage >= minimum_valuation,
        "financial_coverage": financial_coverage >= minimum_finance,
        "last_sync": bool(
            last_sync and last_sync["as_of"] == expected
            and last_sync["status"] == "success" and last_sync["quality_status"] == "ok"
        ),
        "last_selection": bool(
            last_selection and last_selection["status"] == "success"
            and last_selection["market_as_of"] == expected
        ),
        "pit_boundary": bool(
            pit.get("historical_pit_available") and pit.get("market_history_ready")
        ),
    }
    ready = all(checks.values())
    return {
        "service": "shadow-foliant-research",
        "status": "ready" if ready else "degraded",
        "ready": ready,
        "selection_date": selection_date,
        "expected_market_date": expected,
        "actual_market_date": actual,
        "usable_qfq_coverage": round(usable_coverage, 6),
        "valuation_date": valuation_as_of,
        "valuation_coverage": round(valuation_coverage, 6),
        "financial_coverage": round(financial_coverage, 6),
        "last_sync": last_sync,
        "last_selection": last_selection,
        "pit_coverage": pit,
        "calendar": calendar,
        "checks": checks,
    }
