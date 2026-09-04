"""Protected, decision-context-aware readiness for the research warehouse."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Optional

import pandas as pd

from core.decision_context import A_SHARE_TIMEZONE, DecisionContext
from data.research_store import ResearchStore
from data.research_readiness import resolve_valuation


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _default_mode(selection_date: str) -> str:
    now = datetime.now(A_SHARE_TIMEZONE)
    if selection_date != now.date().isoformat():
        return "preopen"
    return "postclose" if now.time() >= time(16, 0) else "preopen"


def snapshot(*, store: Optional[ResearchStore] = None,
             selection_date: Optional[str] = None,
             mode: Optional[str] = None,
             require_selection: bool = True) -> Dict[str, Any]:
    """Return aggregate facts only; never expose symbols, payloads, or credentials."""
    from analysis.local_stock_selector import LocalStockSelector, SelectionPolicy

    store = store or ResearchStore(ensure_schema=False)
    selected = str(selection_date or date.today().isoformat())
    policy = SelectionPolicy.from_env()
    context = DecisionContext.build(
        selected, mode=mode or _default_mode(selected),
        policy_version=policy.version, policy_hash=policy.policy_hash,
    )
    pit = store.pit_coverage(context.universe_cutoff)
    calendar = store.calendar_consensus(
        context.market_cutoff, inclusive=context.market_cutoff_inclusive
    )
    expected = calendar.get("latest_confirmed_open_date")
    universe = store.load_universe(
        context.universe_cutoff, cutoff_at=context.decision_at
    )
    universe_count = int(len(universe.drop_duplicates("symbol"))) if not universe.empty else 0

    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT MAX(trade_date) FROM research_daily_bars
               WHERE trade_date<=? AND adjustment='qfq' AND close>0
               AND quality_status NOT IN ('failed','unknown_unit')""",
            (expected or context.market_cutoff,),
        )
        row = cur.fetchone()
        actual = str(row[0]) if row and row[0] else None
        usable_count = store.daily_bar_symbol_count(actual, adjustment="qfq") if actual else 0
        cur.execute(
            """SELECT as_of,status,quality_status FROM research_sync_runs
               WHERE capability='daily_market' AND as_of<=?
               ORDER BY started_at DESC LIMIT 1""",
            (expected or context.market_cutoff,),
        )
        sync_row = cur.fetchone()
    finally:
        conn.close()

    financial_coverage = 0.0
    valuation_frame, valuation_state = resolve_valuation(
        store, expected or context.market_cutoff,
        universe.get("symbol", pd.Series(dtype=str)),
        min_coverage=policy.min_valuation_coverage, max_lag=policy.max_valuation_lag,
    )
    valuation_as_of = valuation_state["valuation_as_of"]
    try:
        selector = LocalStockSelector(store=store, policy=policy)
        fundamentals = selector._fundamentals(context)
        base = (universe[["symbol", "industry"]].drop_duplicates("symbol")
                if universe_count else pd.DataFrame())
        if not base.empty:
            if not valuation_frame.empty:
                columns = [c for c in ("symbol", "pe_ttm", "pb") if c in valuation_frame]
                base = base.merge(valuation_frame[columns], on="symbol", how="left")
            if not fundamentals.empty:
                base = base.merge(fundamentals, on="symbol", how="left")
            scored = selector._score_fundamentals(base)
            financial_coverage = float(
                (scored["fundamental_metric_count"] >= policy.min_stock_fundamental_metrics).mean()
            )
    except Exception:
        financial_coverage = 0.0

    latest = store.latest_formal_selection()
    last_selection = None
    selection_valid = False
    if latest:
        metadata = latest.get("metadata") or {}
        artifacts = latest.get("artifacts") or {}
        manifest_id = metadata.get("manifest_id")
        selection_valid = bool(
            latest.get("status") == "success"
            and metadata.get("market_as_of") == expected and manifest_id
            and metadata.get("rule_version")
            and artifacts.get("formal_top15") and artifacts.get("formal_top5")
        )
        last_selection = {
            "selection_date": str(latest.get("selection_date") or ""),
            "status": str(latest.get("status") or ""),
            "market_as_of": metadata.get("market_as_of"),
            "rule_version": metadata.get("rule_version"),
            "manifest_present": bool(manifest_id),
            "formal_artifacts_present": bool(
                artifacts.get("formal_top15") and artifacts.get("formal_top5")
            ),
        }

    usable_coverage = _ratio(usable_count, universe_count)
    valuation_coverage = valuation_state["valuation_coverage"]
    last_sync = ({"as_of": str(sync_row[0]), "status": str(sync_row[1]),
                  "quality_status": str(sync_row[2])} if sync_row else None)
    checks = {
        "calendar_consensus": bool(calendar.get("ready") and expected),
        "market_fresh": bool(expected and actual == expected),
        "market_coverage": usable_coverage >= policy.min_warehouse_coverage,
        "valuation_usable": valuation_state["ready"],
        "valuation_coverage": valuation_coverage >= policy.min_valuation_coverage,
        "financial_coverage": financial_coverage >= policy.min_financial_universe_coverage,
        "last_sync": bool(
            last_sync and last_sync["as_of"] == expected
            and last_sync["status"] == "success"
            # Required data are checked individually above. Optional ingestion
            # gaps must not veto an otherwise usable formal decision snapshot.
            and last_sync["quality_status"] in {"ok", "incomplete"}
        ),
        "pit_boundary": bool(
            pit.get("historical_pit_available") and pit.get("market_history_ready")
        ),
    }
    if require_selection:
        checks["formal_selection"] = selection_valid
    ready = all(checks.values())
    ingestion_checks = {
        "valuation_fresh": valuation_state["valuation_fresh"],
        "exact_sync_complete": bool(
            last_sync and last_sync["as_of"] == expected
            and last_sync["status"] == "success" and last_sync["quality_status"] == "ok"
        ),
    }
    return {
        "service": "shadow-foliant-research",
        "kind": "selection" if require_selection else "data",
        "status": "ready" if ready else "degraded", "ready": ready,
        "decision_context": context.as_dict(),
        "expected_market_date": expected, "actual_market_date": actual,
        "usable_qfq_coverage": round(usable_coverage, 6),
        "valuation_date": valuation_as_of,
        "valuation_coverage": round(valuation_coverage, 6),
        "valuation_status": valuation_state["status"],
        "valuation_stale_trading_days": valuation_state["valuation_stale_trading_days"],
        "data_degraded": valuation_state["status"] == "lagged",
        "data_complete": all(
            value for key, value in checks.items() if key != "formal_selection"
        ) and all(ingestion_checks.values()),
        "ingestion_checks": ingestion_checks,
        "financial_coverage": round(financial_coverage, 6),
        "last_sync": last_sync, "last_selection": last_selection,
        "pit_coverage": pit, "calendar": calendar, "checks": checks,
    }


def data_snapshot(**kwargs) -> Dict[str, Any]:
    kwargs["require_selection"] = False
    return cached_snapshot(**kwargs)


def selection_snapshot(**kwargs) -> Dict[str, Any]:
    kwargs["require_selection"] = True
    return cached_snapshot(**kwargs)


def _report_key(store, selected, mode):
    from application.results import payload_hash
    from analysis.local_stock_selector import SelectionPolicy
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT run_id FROM selection_runs WHERE publication_status='published' "
                    "ORDER BY published_at DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        conn.close()
    vector = store.generation_vector()
    identity = {"selection_date": selected, "mode": mode, "generations": vector,
                "formal_run": str(row[0]) if row else None,
                "policy_hash": SelectionPolicy.from_env().policy_hash}
    return payload_hash(identity), identity


def refresh_quality_report(*, store=None, selection_date=None, mode=None):
    """Job-side computation only. HTTP readers never trigger full-market scoring."""
    import json
    from application.results import payload_hash
    store = store or ResearchStore(ensure_schema=False)
    selected = str(selection_date or date.today().isoformat())
    mode = mode or _default_mode(selected)
    key, identity = _report_key(store, selected, mode)
    report = snapshot(store=store, selection_date=selected, mode=mode, require_selection=True)
    after, _ = _report_key(store, selected, mode)
    if after != key:
        return {"status": "stale", "ready": False, "reason": "generation_changed_during_evaluation"}
    report.update(report_id=key, identity=identity, evaluated_at=datetime.now(A_SHARE_TIMEZONE).isoformat())
    encoded = json.dumps(report, ensure_ascii=False, default=str, allow_nan=False)
    revision = payload_hash(report)
    conn = store.connect()
    try:
        conn.execute("INSERT INTO research_artifacts "
                     "(artifact_id,subject,artifact_kind,run_id,formal,schema_version,payload_hash,payload,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,artifact_kind) DO NOTHING",
                     ("qr_" + revision, f"quality:{key}", "quality-report", revision, 0,
                      "quality-report-v1", payload_hash(report), encoded, report["evaluated_at"]))
        conn.commit()
    finally:
        conn.close()
    return report


def cached_snapshot(*, store=None, selection_date=None, mode=None, require_selection=True):
    import json
    store = store or ResearchStore(ensure_schema=False)
    selected = str(selection_date or date.today().isoformat())
    mode = mode or _default_mode(selected)
    key, identity = _report_key(store, selected, mode)
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT payload FROM research_artifacts WHERE subject=? AND artifact_kind='quality-report' "
                    "ORDER BY created_at DESC LIMIT 1", (f"quality:{key}",))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"service": "shadow-foliant-research", "status": "stale", "ready": False,
                "kind": "selection" if require_selection else "data", "identity": identity,
                "checks": {"quality_report_current": False},
                "reason": "quality_report_missing_or_generation_changed"}
    report = dict(row[0]) if isinstance(row[0], dict) else json.loads(row[0])
    age = (datetime.now(A_SHARE_TIMEZONE) - datetime.fromisoformat(report["evaluated_at"])).total_seconds()
    if not 0 <= age <= 86400:
        return {"service": "shadow-foliant-research", "status": "stale", "ready": False,
                "checks": {"quality_report_current": False}, "reason": "quality_report_expired"}
    report["checks"] = dict(report["checks"])
    if not require_selection:
        report["checks"].pop("formal_selection", None)
    report["kind"] = "selection" if require_selection else "data"
    report["ready"] = all(report["checks"].values())
    report["status"] = "ready" if report["ready"] else "degraded"
    return report
