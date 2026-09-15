"""Bounded, read-only snapshot for repository-external scheduled consumers."""

from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from application.account_preview import (
    account_quote_symbols,
    build_account_preview,
)
from application.results import clean_json, payload_hash, provenance, tool_result
from application.stock_budget import (
    STOCK_BUDGET_BASIS,
    default_metadata_reader,
    derive_stock_budget,
)


EXPECTED_WENCAI_STRATEGIES = (
    "低价擒牛", "低估值", "主力资金", "小市值", "净利增长",
)
POST_CLOSE_REVIEW_HOUR = 20
POST_CLOSE_REVIEW_MINUTE = 45
POST_CLOSE_JOBS = (
    "portfolio_indicator_snapshot", "eod_outcomes", "daily_backtest",
    "research_data_sync_retry",
)
MIN_STRATEGY_FEEDBACK_SAMPLES = 30
MIN_PERFORMANCE_FACTOR_DEVIATION = 0.05
MAX_STRATEGY_MULTIPLIER_STEP = 0.05
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+\S+|postgres(?:ql)?://\S+|https?://\S+|"
    r"(?:token|secret|password|cookie)\s*[:=]\s*\S+|"
    r"server at\s+[\"'][^\"']+[\"'](?:,\s*port\s+\d+)?)"
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub("[redacted]", value)
    return value


def _status(value: Any, default: str = "missing") -> str:
    state = str((value or {}).get("status") or default).lower() if isinstance(value, dict) else default
    return state if state in {
        "complete", "success", "stale", "missing", "degraded", "partial",
        "pending", "not_applicable", "blocked", "review_required",
    } else default


def _candidate(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    allowed = (
        "symbol", "code", "name", "rank", "score", "total_score", "final_score",
        "assigned_lane", "source_labels", "technical_state", "trade_plan",
        "score_components", "tradeability", "data_quality",
    )
    value = {key: row.get(key) for key in allowed if key in row}
    symbol = str(value.get("symbol") or value.get("code") or "")
    value["symbol"] = symbol
    value.pop("code", None)
    return clean_json(value)


def _trade_plan(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    allowed = (
        "available", "action", "action_cn", "market_action", "entry_low", "entry_high",
        "stop_loss", "target_price", "target_price_2", "current_price", "plan_as_of",
        "price_basis", "horizon", "horizon_cn", "risk_reward_ratio",
        "suggested_position_pct", "blockers",
    )
    return clean_json({key: row.get(key) for key in allowed if key in row})


def _holding(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    allowed = ("code", "symbol", "name", "cost_price", "quantity", "created_at", "updated_at")
    value = {key: row.get(key) for key in allowed if key in row}
    value["symbol"] = str(value.get("symbol") or value.get("code") or "")
    value.pop("code", None)
    return clean_json(value)


def _quote(symbol: str, row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        row = {}
    allowed = (
        "name", "price", "change_pct", "open", "high", "low", "volume", "amount_wan",
        "quote_time", "observed_at", "retrieved_at", "quote_time_source", "source",
        "suspended", "limit_up", "limit_down",
    )
    value = {key: row.get(key) for key in allowed if key in row}
    value["symbol"] = symbol
    value["as_of"] = (row.get("quote_time") or row.get("observed_at")
                      or row.get("retrieved_at"))
    return clean_json(value)


def _action_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing", "preview_only": True}
    allowed = (
        "schema_version", "status", "error_code", "preview_only", "summary", "created_at",
        "expires_at", "cash_basis", "blockers", "missing_information", "risk_snapshot",
        "stress_scenarios", "actual_formal_difference", "alternatives", "rejected_candidates",
        "pricing_snapshot",
    )
    return clean_json({key: value.get(key) for key in allowed if key in value})


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _job_run(row: Any) -> dict[str, Any]:
    """Project a job run without exposing free-form error text."""
    if not isinstance(row, dict):
        return {}
    raw_status = str(row.get("status") or "missing").lower()
    status = raw_status if raw_status in {
        "success", "error", "failed", "skipped", "running", "pending",
    } else "missing"
    metrics: dict[str, Any] = {}
    detail = str(row.get("error") or "")
    patterns = {
        "recommendations_checked": r"\bchecked=(\d+)",
        "recommendation_targets_hit": r"\btp=(\d+)",
        "recommendation_stops_hit": r"\bsl=(\d+)",
        "signals_evaluated": r"\beval=(\d+)",
        "signals_hit": r"\bhit=(\d+)",
        "signals_missed": r"\bmiss=(\d+)",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, detail)
        if match:
            metrics[name] = int(match.group(1))
    partial_failures = [
        code for code, marker in (
            ("recommendation_outcome_failed", "rec_err="),
            ("decision_signal_outcome_failed", "signal_err="),
            ("decision_loop_failed", "decision_loop_err="),
        )
        if marker in detail
    ]
    if partial_failures and status == "success":
        status = "degraded"
    completion_semantics = "required_success"
    if (str(row.get("job_name") or "") == "research_data_sync_retry"
            and raw_status == "skipped" and "already complete" in detail.lower()):
        status = "not_applicable"
        completion_semantics = "upstream_daily_market_already_complete"
    decision_loop = re.search(r"\bdecision_loop=([a-zA-Z0-9_-]+)", detail)
    if decision_loop:
        metrics["decision_loop_status"] = decision_loop.group(1)[:40]
    return clean_json({
        "job_name": row.get("job_name"),
        "status": status,
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "metrics": metrics,
        "partial_failure_codes": partial_failures,
        "completion_semantics": completion_semantics,
    })


def _report_phase(now: datetime, trading_day: dict[str, Any]) -> str:
    if trading_day.get("confirmed") and not trading_day.get("is_trading_day"):
        return "closed_day"
    minute = now.hour * 60 + now.minute
    if minute < 15 * 60:
        return "intraday"
    if minute < POST_CLOSE_REVIEW_HOUR * 60 + POST_CLOSE_REVIEW_MINUTE:
        return "post_close_pending"
    return "post_close_review"


def _quote_coverage_complete(quotes: dict[str, Any] | None) -> bool:
    quotes = quotes or {}
    requested = int(quotes.get("requested_count") or 0)
    available = int(quotes.get("available_count") or 0)
    return (requested > 0 and available == requested
            and str(quotes.get("status") or "") in {"complete", "success"})


def _optional_quote_provider_degradations(
        cockpit: dict[str, Any], quotes: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Report a failed auxiliary quote source without weakening coverage truth."""
    if not _quote_coverage_complete(quotes):
        return []
    sources = ((cockpit.get("datahub") or {}).get("sources") or {})
    primary = sources.get("quotes:a_stock") or {}
    auxiliary = sources.get("quotes:fuyao_aicubes") or {}
    if int(primary.get("ok") or 0) < 1 or int(auxiliary.get("streak_fail") or 0) < 1:
        return []
    return [{
        "source": "quotes:fuyao_aicubes",
        "role": "auxiliary",
        "fallback_source": "quotes:a_stock",
        "fallback_coverage": "complete",
        "failure_code": auxiliary.get("failure_code"),
        "failure_category": auxiliary.get("failure_category"),
        "http_status": auxiliary.get("http_status"),
    }]


def _cockpit_quality_for_phase(
        cockpit: dict[str, Any], phase: str, quotes: dict[str, Any] | None = None) -> str:
    """Classify real blockers separately from covered auxiliary-source failures."""
    state = str(cockpit.get("status") or "missing")
    reasons = set(cockpit.get("degradation_reasons") or [])
    if (state == "degraded" and reasons
            and reasons <= {"optional_quote_provider_degraded"}
            and _quote_coverage_complete(quotes)):
        return "complete"
    if phase not in {"post_close_pending", "post_close_review", "closed_day"}:
        return state
    tasks = cockpit.get("tasks") or {}
    if (state == "degraded"
            and not (tasks.get("failed_recent") or [])
            and not (tasks.get("disabled_core") or [])
            and not (tasks.get("running_manual") or [])):
        policy = cockpit.get("portfolio_policy") or {}
        signal = policy.get("market_add_signal") or {}
        if policy.get("fail_closed") and signal.get("stale"):
            return "complete"
    return state


def _outcome_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing", "dimension": "source_type", "buckets": []}
    allowed = (
        "bucket", "bucket_cn", "n", "hit", "miss", "neutral", "win_rate_pct",
        "avg_ret_pct", "directional_n", "minimum_feedback_samples", "sample_status",
        "posterior_hit_rate_pct", "performance_factor",
    )
    buckets = [
        clean_json({key: row.get(key) for key in allowed if key in row})
        for row in (value.get("buckets") or [])[:100]
        if isinstance(row, dict)
    ]
    return {
        "status": "degraded" if value.get("error") else "complete",
        "dimension": "source_type",
        "lookback_days": value.get("days"),
        "buckets": buckets,
    }


def _strategy_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing", "strategies": []}
    allowed = (
        "strategy_id", "lane", "strategy_version", "metric_version", "sample_size",
        "independent_dates", "nonoverlapping_price_intervals", "effective_samples",
        "promotion_blocker", "symbol_count", "win_rate_pct", "avg_return_pct",
        "worst_drawdown_pct", "worst_mae_pct",
    )
    strategies = [
        clean_json({key: row.get(key) for key in allowed if key in row})
        for row in (value.get("strategies") or [])[:100]
        if isinstance(row, dict)
    ]
    comparison = value.get("portfolio_comparison") or {}
    return clean_json({
        "status": "complete",
        "horizon_days": value.get("horizon_days"),
        "lookback_days": value.get("lookback_days"),
        "evidence_snapshot_id": value.get("evidence_snapshot_id"),
        "strategies": strategies,
        "portfolio_comparison": {
            "matured_runs": comparison.get("matured_runs"),
            "avg_satellite_marginal_pct": comparison.get("avg_satellite_marginal_pct"),
        },
    })


class ScheduledSnapshotService:
    """Compose existing use cases without introducing a second data-access path."""

    def __init__(
        self,
        *,
        store: Any = None,
        selection_reader: Callable[[], dict[str, Any]] | None = None,
        cockpit_reader: Callable[..., dict[str, Any]] | None = None,
        context_reader: Callable[[], dict[str, Any]] | None = None,
        capsule_reader: Callable[[], dict[str, Any] | None] | None = None,
        intraday_reader: Callable[[], dict[str, Any]] | None = None,
        intraday_projector: Callable[..., dict[str, Any]] | None = None,
        external_research_reader: Callable[[], dict[str, Any]] | None = None,
        quote_loader: Callable[[list[str]], dict[str, Any]] | None = None,
        job_runs_reader: Callable[..., list[dict[str, Any]]] | None = None,
        outcome_stats_reader: Callable[..., dict[str, Any]] | None = None,
        strategy_evidence_reader: Callable[..., dict[str, Any]] | None = None,
        cash_reader: Callable[[], dict[str, Any]] | None = None,
        security_metadata_reader: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.selection_reader = selection_reader
        self.cockpit_reader = cockpit_reader
        self.context_reader = context_reader
        self.capsule_reader = capsule_reader
        self.intraday_reader = intraday_reader
        self.intraday_projector = intraday_projector
        self.external_research_reader = external_research_reader
        self.quote_loader = quote_loader
        self.job_runs_reader = job_runs_reader
        self.outcome_stats_reader = outcome_stats_reader
        self.strategy_evidence_reader = strategy_evidence_reader
        self.cash_reader = cash_reader
        self.security_metadata_reader = security_metadata_reader
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))

    def _project_intraday_from_loaded_facts(
        self, *, now: datetime, context: dict[str, Any], raw_quotes: dict[str, Any],
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-evaluate intraday actions with this request's single quote batch.

        The projection is deliberately read-only: it neither persists trigger state
        nor emits notifications. This prevents a scheduled report from combining a
        newly fetched quote batch with holding actions generated by an older batch.
        """
        if self.intraday_projector is not None:
            return self.intraday_projector(
                now=now, context=context, raw_quotes=raw_quotes, previous=previous,
            ) or {}
        try:
            raw_formal = self.store.latest_formal_selection() or {}
            if not raw_formal:
                return {}
            from jobs import jobs_hub
            from jobs.intraday_decision_monitor import SNAPSHOT_KEY, run_cycle

            def snapshot_loader(key: str) -> dict[str, Any]:
                if key == SNAPSHOT_KEY:
                    return previous
                return jobs_hub.get_indicator_snapshot(key) or {}

            return run_cycle(
                now=now,
                allow_plan_build=False,
                notify_changes=False,
                formal_loader=lambda: raw_formal,
                holdings_loader=lambda: list(context.get("holdings") or []),
                quote_loader=lambda symbols: {
                    symbol: raw_quotes.get(symbol) or raw_quotes.get(str(symbol).zfill(6)) or {}
                    for symbol in symbols
                },
                snapshot_loader=snapshot_loader,
                snapshot_saver=lambda _key, _value: None,
            )
        except Exception:
            return {}

    def _dependencies(self):
        if self.store is None:
            from data.research_store import ResearchStore

            self.store = ResearchStore(ensure_schema=False)
        if self.selection_reader is None:
            from application.services import SelectionRunService

            self.selection_reader = SelectionRunService(store=self.store).latest_formal
        if self.cockpit_reader is None:
            from jobs.task_control import agent_cockpit

            self.cockpit_reader = agent_cockpit
        if self.context_reader is None:
            from portfolio_db import portfolio_db

            self.context_reader = portfolio_db.action_preview_context
        if self.capsule_reader is None:
            from application.decision_loop import DecisionLoopService

            self.capsule_reader = DecisionLoopService(self.store).capsule
        if self.intraday_reader is None:
            from jobs.intraday_decision_monitor import latest_snapshot

            self.intraday_reader = latest_snapshot
        if self.external_research_reader is None:
            from application.external_research import ExternalIndependentResearchService

            self.external_research_reader = ExternalIndependentResearchService(
                store=self.store
            ).latest_data
        if self.quote_loader is None:
            import datahub

            self.quote_loader = datahub.quotes
        if self.job_runs_reader is None:
            from jobs.task_control import recent_scheduled_runs

            self.job_runs_reader = recent_scheduled_runs
        if self.outcome_stats_reader is None:
            from analysis.decision_signal import outcome_stats

            self.outcome_stats_reader = outcome_stats
        if self.strategy_evidence_reader is None:
            self.strategy_evidence_reader = self.store.selection_strategy_evidence
        if self.cash_reader is None:
            from application.account_reconciliation import AccountReconciliation

            reconciliation = AccountReconciliation(self.store)
            self.cash_reader = lambda: reconciliation.latest_cash_balance(
                owner="portfolio-primary"
            )
        if self.security_metadata_reader is None:
            self.security_metadata_reader = default_metadata_reader(self.store)

    @staticmethod
    def _missing(code: str, hint: str) -> dict[str, Any]:
        return {"status": "missing", "error_code": code, "repair_hint": hint}

    def _trading_day(self, today: str) -> dict[str, Any]:
        try:
            consensus = self.store.calendar_consensus(today, inclusive=True)
        except Exception:
            return self._missing(
                "calendar_evidence_unavailable",
                "Restore PostgreSQL access and refresh two-source trade-calendar evidence.",
            ) | {"date": today, "confirmed": False, "is_trading_day": None,
                 "basis": "unavailable"}
        coverage = str(consensus.get("coverage_through_date") or "")
        confirmed = bool(
            consensus.get("ready")
            and int(consensus.get("covered_provider_count") or 0) >= 2
            and coverage >= today
        )
        if not confirmed:
            return self._missing(
                "calendar_consensus_incomplete",
                "Refresh and persist two independent calendar sources through the target date.",
            ) | {"date": today, "confirmed": False, "is_trading_day": None,
                 "basis": "two_source_consensus_required", "as_of": coverage or None,
                 "provider_count": int(consensus.get("covered_provider_count") or 0)}
        latest_open = consensus.get("latest_confirmed_open_date")
        return {
            "status": "complete",
            "date": today,
            "confirmed": True,
            "is_trading_day": str(latest_open or "") == today,
            "latest_confirmed_open_date": latest_open,
            "basis": "two_source_calendar_consensus",
            "as_of": coverage,
            "provider_count": int(consensus.get("covered_provider_count") or 0),
        }

    def _cockpit(self) -> dict[str, Any]:
        try:
            value = self.cockpit_reader(recent_limit=5, compact=True) or {}
        except Exception:
            return self._missing(
                "cockpit_unavailable", "Check the Foliant application database and jobs runtime.",
            )
        data = value.get("data") or {}
        tasks = data.get("tasks") or {}
        projected = {
            "status": _status(value, "degraded"),
            "tasks": {
                "total": tasks.get("total"),
                "failed_recent": [
                    {key: row.get(key) for key in ("name", "status", "at")}
                    for row in (tasks.get("failed_recent") or [])[:5]
                    if isinstance(row, dict)
                ],
                "disabled_core": list(tasks.get("disabled_core") or [])[:20],
                "running_manual": [
                    {key: row.get(key) for key in ("task_name", "run_id", "status")}
                    for row in (tasks.get("running_manual") or [])[:10]
                    if isinstance(row, dict)
                ],
            },
            "holding_count": data.get("holding_count"),
            "active_recommendation_count": data.get("active_recommendation_count"),
            "active_signal_count": data.get("active_signal_count"),
            "datahub": data.get("datahub"),
            "portfolio_policy": data.get("portfolio_policy"),
            "degradation_reasons": list(data.get("degradation_reasons") or [])[:20],
            "blocking_dimensions": list(data.get("blocking_dimensions") or [])[:20],
            "strategy_deployment": data.get("strategy_deployment"),
            "as_of": (value.get("meta") or {}).get("as_of"),
        }
        return clean_json(projected)

    @staticmethod
    def _formal(value: dict[str, Any], trading_day: dict[str, Any]) -> dict[str, Any]:
        data = value.get("data") or {}
        top15 = [_candidate(row) for row in data.get("formal_top15") or []]
        top5 = [_candidate(row) for row in data.get("formal_top5") or []]
        state = _status(value)
        selection_date = str(data.get("selection_date") or "")
        expected = str(trading_day.get("latest_confirmed_open_date") or "")
        warnings = list(value.get("warnings") or [])
        if state in {"complete", "success"} and expected and selection_date < expected:
            state = "stale"
            warnings.append("formal selection predates the latest confirmed open date")
        return {
            "status": "complete" if state == "success" else state,
            "selection_date": selection_date or None,
            "market_as_of": (value.get("provenance") or {}).get("market_as_of"),
            "run_id": (value.get("provenance") or {}).get("run_id"),
            "formal_top15": top15[:15],
            "formal_top5": top5[:5],
            "as_of": value.get("provenance") or {},
            "warnings": [str(item)[:300] for item in warnings[:10]],
        }

    @staticmethod
    def _wencai(selection_value: dict[str, Any]) -> dict[str, Any]:
        payload = (((selection_value.get("data") or {}).get("references") or {}).get("wencai") or {})
        strategies = payload.get("strategies") or {}
        rows = []
        for name in EXPECTED_WENCAI_STRATEGIES:
            raw = strategies.get(name) or {}
            picks = [_candidate(row) for row in (raw.get("picks") or [])]
            rows.append({
                "name": name,
                "strategy_id": raw.get("strategy_id"),
                "strategy_version": raw.get("strategy_version"),
                "status": str(raw.get("status") or "missing"),
                "failure_code": raw.get("failure_code"),
                "started_at": raw.get("started_at"),
                "finished_at": raw.get("finished_at"),
                "elapsed_seconds": raw.get("elapsed_seconds"),
                "cache_key": raw.get("cache_key"),
                "cache_age_seconds": raw.get("cache_age_seconds"),
                "result_as_of": raw.get("result_as_of"),
                "circuit_scope": raw.get("circuit_scope"),
                "picks": picks[:15],
            })
        present = sum(1 for row in rows if row["status"] != "missing")
        ready = sum(1 for row in rows if row["status"] == "ready")
        try:
            import datahub

            fuyao = clean_json(datahub.fuyao_capabilities() or {})
        except Exception:
            fuyao = {"configured": False, "enabled": False, "capabilities": {}}
        fuyao_configured = bool(fuyao.get("configured") and fuyao.get("enabled"))
        return {
            "status": "missing" if present == 0 else "complete" if ready == 5 else "degraded",
            "provider": "iwencai_reference_adapter",
            "availability": "ready" if ready == 5 else "long_term_degraded",
            "reference_only": True,
            "reference_affects_membership": False,
            "blocks_snapshot_quality": False,
            "expected_groups": 5,
            "present_groups": present,
            "ready_groups": ready,
            "strategies": rows,
            "as_of": payload.get("executed_at"),
            "degradation_boundary": {
                "formal_selection_unaffected": True,
                "no_synthetic_results": True,
                "retry_policy": "bounded_scheduled_attempts_only",
            },
            "official_replacement_contract": {
                "provider": "fuyao_aicubes",
                "status": "configured" if fuyao_configured else "not_configured",
                "identity": "distinct_official_provider_not_wencai",
                "must_preserve_provider_identity": True,
                "must_not_be_labeled_as_wencai": True,
                "dimensions": {
                    "capital_flow": (fuyao.get("capital_flow") or {}).get("status")
                                    or "degraded",
                    "valuation": "connected" if fuyao_configured else "not_configured",
                    "financial_growth": "connected" if fuyao_configured else "not_configured",
                    "market_attention": "connected" if fuyao_configured else "not_configured",
                    "tradeability": "connected" if fuyao_configured else "not_configured",
                },
            },
        }

    @staticmethod
    def _independent(
        selection_value: dict[str, Any],
        expected_market_as_of: str | None = None,
    ) -> dict[str, Any]:
        data = selection_value.get("data") or {}
        payload = ((data.get("references") or {}).get("independent") or {})
        ready = payload.get("status") == "ready"
        expected = str(expected_market_as_of or "").strip()
        market_as_of = str(payload.get("market_as_of") or "").strip()
        status = "complete" if ready else "missing"
        reason = payload.get("reason")
        warnings: list[str] = []
        if status == "complete" and expected:
            if market_as_of != expected:
                status = "stale"
                reason = reason or "independent result date mismatch"
                warnings.append(
                    f"independent_selection.market_as_of({market_as_of or 'missing'}) != "
                    f"formal_selection_date({expected})"
                )
        return {
            "status": status,
            "availability": payload.get("status") or "unavailable",
            "reason": reason,
            "strategy_id": payload.get("strategy_id"),
            "strategy_version": payload.get("strategy_version"),
            "strategy_hash": payload.get("strategy_hash"),
            "manifest_id": payload.get("manifest_id"),
            "input_snapshot_id": payload.get("input_snapshot_id"),
            "input_provenance": clean_json(payload.get("input_provenance") or {}),
            "market_as_of": payload.get("market_as_of"),
            "expected_market_as_of": expected,
            "weights": clean_json(payload.get("weights") or {}),
            "top15": [_candidate(row) for row in (payload.get("top15") or [])][:15],
            "top5": [_candidate(row) for row in (payload.get("top5") or [])][:5],
            "independence_boundary": payload.get("independence_boundary"),
            "comparison": clean_json(data.get("selection_comparison") or {}),
            "warnings": [item[:300] for item in warnings][:10],
        }

    @staticmethod
    def _candidate_follow_up(
        formal: dict[str, Any], quote_rows: list[dict[str, Any]],
        trade_plans: dict[str, dict[str, Any]] | None = None,
        stock_budget: dict[str, Any] | None = None,
        phase: str = "intraday",
    ) -> list[dict[str, Any]]:
        quote_by_symbol = {str(row.get("symbol") or ""): row for row in quote_rows}
        trade_plans = trade_plans or {}
        stock_budget = stock_budget or {}
        result = []
        for candidate in formal.get("formal_top15") or []:
            symbol = str(candidate.get("symbol") or "")
            quote = quote_by_symbol.get(symbol) or {}
            embedded_plan = candidate.get("trade_plan") or {}
            persisted_plan = trade_plans.get(symbol) or {}
            trade_plan = embedded_plan or persisted_plan
            plan_source = (
                "formal_artifact" if embedded_plan else
                "current_intraday_snapshot" if persisted_plan and phase == "intraday" else
                "historical_intraday_reference" if persisted_plan else
                "missing"
            )
            plan_available = bool(trade_plan) and trade_plan.get("available") is not False
            blockers = []
            if formal.get("status") not in {"complete", "success"}:
                blockers.append("formal_selection_not_current")
            if quote.get("freshness") not in {"actionable", "closing_current"}:
                blockers.append("quote_stale_or_missing")
            if not plan_available:
                blockers.append("trade_plan_missing")
            budget_blockers = list(stock_budget.get("buy_side_blockers") or [])
            blockers.extend(item for item in budget_blockers if item not in blockers)
            available_cash = _finite_number(stock_budget.get("available_cash_cny"))
            total_budget = _finite_number(stock_budget.get("total_budget_cny"))
            quote_price = _finite_number(quote.get("price"))
            max_order_value = (
                min(available_cash, total_budget * 0.15, total_budget * 0.10)
                if available_cash is not None and total_budget is not None else None
            )
            max_quantity = None
            if max_order_value is not None and quote_price and quote_price > 0:
                try:
                    from analysis.decision_evaluation import equity_rules

                    rules = equity_rules(symbol)
                    step = int(rules.buy_step) if rules else 100
                    max_quantity = int(max_order_value / quote_price) // step * step
                except Exception:
                    max_quantity = None
            result.append(clean_json({
                "symbol": symbol,
                "name": candidate.get("name"),
                "rank": candidate.get("rank"),
                "status": "blocked" if blockers else "ready",
                "quote_as_of": quote.get("as_of"),
                "quote_freshness": quote.get("freshness") or "stale_or_missing",
                "trade_plan_available": plan_available,
                "trade_plan_source": plan_source,
                "trade_plan": _trade_plan(trade_plan),
                "stock_budget_constraint": {
                    "status": "ready" if not budget_blockers else "blocked",
                    "cash_basis": stock_budget.get("basis"),
                    "available_cash_cny": available_cash,
                    "max_order_value_cny": (
                        round(max_order_value, 2) if max_order_value is not None else None
                    ),
                    "max_quantity_before_fees": max_quantity,
                    "position_limit_pct": 15,
                    "turnover_limit_pct": 10,
                    "preview_only": True,
                },
                "blockers": blockers,
            }))
        return result[:15]

    @staticmethod
    def _next_premarket_check(
        formal: dict[str, Any], independent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checks = [
            "refresh_two_source_calendar_consensus",
            "revalidate_formal_selection_freshness",
            "refresh_batch_quotes_and_tradeability",
            "confirm_cash_and_sellable_quantities",
            "rebuild_preview_if_portfolio_watermark_changed",
        ]
        if (independent or {}).get("status") not in {"complete", "success", "missing", "not_applicable"}:
            checks.append("revalidate_independent_selection_freshness")
        return {
            "status": "required" if formal.get("formal_top15") else "missing",
            "target_session_date": None,
            "date_basis": "next_confirmed_open_date_requires_fresh_two_source_consensus",
            "checks": checks[:10],
            "preview_only": True,
            "auto_execution": False,
        }

    @staticmethod
    def _source_comparison(
        selection_value: dict[str, Any], formal: dict[str, Any],
        independent: dict[str, Any], wencai: dict[str, Any],
    ) -> dict[str, Any]:
        raw = clean_json(
            ((selection_value.get("data") or {}).get("selection_comparison") or {})
        )
        availability = {
            "formal": formal.get("status") in {"complete", "success"}
                      and bool(formal.get("formal_top15")),
            "independent": independent.get("status") in {"complete", "success"},
            "wencai": int(wencai.get("ready_groups") or 0) == len(EXPECTED_WENCAI_STRATEGIES),
        }
        pair_dependencies = {
            "formal_independent": ("formal", "independent"),
            "formal_wencai": ("formal", "wencai"),
            "independent_wencai": ("independent", "wencai"),
        }
        raw_pairs = raw.get("pairwise") or {}
        pairs = {
            name: clean_json(raw_pairs.get(name) or {})
            for name, required in pair_dependencies.items()
            if all(availability[source] for source in required) and raw_pairs.get(name)
        }
        return clean_json({
            "status": "complete" if availability["formal"] else "degraded",
            "selection_date": formal.get("selection_date"),
            "availability": availability,
            "unavailable_sources": [name for name, ready in availability.items() if not ready],
            "pairwise": pairs,
            "triple": raw.get("triple") if all(availability.values()) else None,
            "reference_only": True,
            "formal_membership_unchanged": True,
        })

    @staticmethod
    def _external_research(
        raw: dict[str, Any], *, formal: dict[str, Any], independent: dict[str, Any],
        wencai: dict[str, Any],
    ) -> dict[str, Any]:
        raw = clean_json(raw or {})
        overlay = raw.get("overlay") or {}
        current = bool(
            raw.get("status") == "ready"
            and overlay.get("selection_run_id") == formal.get("run_id")
            and overlay.get("base_strategy_version") == independent.get("strategy_version")
            and overlay.get("base_input_snapshot_id") == independent.get("input_snapshot_id")
        )
        external_top15 = [_candidate(row) | {
            "base_rank": row.get("base_rank"),
            "base_score": row.get("base_score"),
            "event_adjustment": row.get("event_adjustment"),
            "risk_veto": bool(row.get("risk_veto")),
            "final_score": row.get("final_score"),
            "evidence_ids": list(row.get("evidence_ids") or [])[:100],
        } for row in (overlay.get("top15") or [])][:15]
        formal_top5 = {str(row.get("symbol") or "") for row in formal.get("formal_top5") or []}
        independent_top5 = {
            str(row.get("symbol") or "") for row in independent.get("top5") or []
        }
        external_top5 = {str(row.get("symbol") or "") for row in external_top15[:5]}
        wencai_top = {
            str(row.get("symbol") or "")
            for group in wencai.get("strategies") or []
            for row in group.get("picks") or []
        } if int(wencai.get("ready_groups") or 0) == len(EXPECTED_WENCAI_STRATEGIES) else set()
        comparison = {
            "formal_external_top5": sorted(formal_top5 & external_top5),
            "independent_external_top5": sorted(independent_top5 & external_top5),
            "wencai_external_top5": sorted(wencai_top & external_top5) if wencai_top else None,
        }
        return clean_json({
            "status": "complete" if current else "stale" if overlay else "missing",
            "channel": raw.get("channel") or "codex-external-independent-v1",
            "overlay_id": overlay.get("overlay_id"),
            "idempotency_key": overlay.get("idempotency_key")
            or (raw.get("idempotency") or {}).get("key"),
            "selection_run_id": overlay.get("selection_run_id"),
            "decision_as_of": overlay.get("decision_as_of"),
            "ranking_locked_at": overlay.get("ranking_locked_at"),
            "market_regime": overlay.get("market_regime"),
            "top15": external_top15,
            "top5": external_top15[:5],
            "evidence": list(raw.get("evidence") or [])[:100],
            "news_watchlist": list(raw.get("news_watchlist") or [])[:100],
            "tuning_proposals": list(raw.get("tuning_proposals") or [])[:50],
            "outcomes": raw.get("outcomes") or {"buckets": []},
            "comparison": comparison,
            "identity_boundary": overlay.get("identity_boundary"),
            "formal_membership_unchanged": True,
            "external_can_create_execution_price": False,
            "human_review_required": True,
            "auto_apply": False,
            "auto_execution": False,
        })

    @staticmethod
    def _holdings_review(
        *, due: bool, trading_day: dict[str, Any], holdings: list[dict[str, Any]],
        quote_rows: list[dict[str, Any]], plans: dict[str, dict[str, Any]],
        pricing_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        base = {
            "review_date": trading_day.get("date"),
            "pricing_snapshot": pricing_snapshot,
            "preview_only": True,
            "auto_execution": False,
        }
        if not due:
            return base | {"status": "pending", "rows": []}
        if trading_day.get("confirmed") and not trading_day.get("is_trading_day"):
            return base | {"status": "not_applicable", "rows": []}
        quote_by_symbol = {str(row.get("symbol") or ""): row for row in quote_rows}
        rows = []
        for holding in holdings:
            symbol = str(holding.get("symbol") or holding.get("code") or "")
            quote = quote_by_symbol.get(symbol) or {}
            freshness = str(quote.get("freshness") or "stale_or_missing")
            close_price = (
                _finite_number(quote.get("price"))
                if freshness in {"actionable", "closing_current"} else None
            )
            cost = _finite_number(holding.get("cost_price"))
            pnl = (
                round((close_price - cost) / cost * 100, 2)
                if close_price is not None and cost and cost > 0 else None
            )
            plan = plans.get(symbol) or {}
            stop = _finite_number(plan.get("stop_loss"))
            target = _finite_number(plan.get("target_price"))
            if close_price is None:
                action, reason, state = "data_insufficient", "当日收盘价不可用", "blocked"
            elif stop is not None and close_price <= stop:
                action, reason, state = "sell", "收盘价触及计划止损", "reviewed"
            elif target is not None and close_price >= target:
                action, reason, state = "reduce", "收盘价触及计划第一目标", "reviewed"
            elif str(plan.get("action") or "") in {"sell", "reduce"}:
                action = str(plan.get("action"))
                reason = str(plan.get("reason") or "沿用规则计划的风险动作")[:300]
                state = "reviewed"
            else:
                action, reason, state = "hold", "收盘未触及止损或止盈阈值", "reviewed"
            blockers = []
            if close_price is None:
                blockers.append("closing_quote_unavailable")
            if not plan or plan.get("available") is False:
                blockers.append("trade_plan_missing")
            rows.append(clean_json({
                "symbol": symbol,
                "name": holding.get("name"),
                "quantity": holding.get("quantity"),
                "cost_price": cost,
                "reference_close": close_price,
                "quote_as_of": quote.get("as_of"),
                "quote_freshness": freshness,
                "holding_pnl_pct": pnl,
                "status": "degraded" if blockers else state,
                "action": action,
                "action_cn": {
                    "hold": "不动", "reduce": "减仓", "sell": "卖出",
                    "data_insufficient": "数据不足",
                }[action],
                "reason": reason,
                "stop_loss": stop,
                "target_price": target,
                "blockers": blockers,
            }))
        complete = all(row.get("status") == "reviewed" for row in rows)
        return clean_json(base | {
            "status": "complete" if complete else "degraded",
            "count": len(rows),
            "rows": rows[:100],
            "price_basis": "same_trading_day_close_snapshot",
        })

    @staticmethod
    def _next_session_plan(
        *, due: bool, trading_day: dict[str, Any], formal: dict[str, Any],
        holdings: list[dict[str, Any]], quote_rows: list[dict[str, Any]],
        plans: dict[str, dict[str, Any]], pricing_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        base = {
            "based_on_session": trading_day.get("date"),
            "pricing_snapshot": pricing_snapshot,
            "target_session_date": None,
            "target_date_basis": "next_confirmed_open_date_requires_fresh_two_source_consensus",
            "preview_only": True,
            "auto_execution": False,
            "execution_preconditions": [
                "refresh_next_session_quotes",
                "confirm_cash_and_sellable_quantities",
                "revalidate_portfolio_watermark",
            ],
        }
        if not due:
            return base | {"status": "pending", "rows": []}
        if trading_day.get("confirmed") and not trading_day.get("is_trading_day"):
            return base | {"status": "not_applicable", "rows": []}
        quote_by_symbol = {str(row.get("symbol") or ""): row for row in quote_rows}
        candidates = {str(row.get("symbol") or ""): row for row in formal.get("formal_top15") or []}
        items: dict[str, dict[str, Any]] = {}
        for holding in holdings:
            symbol = str(holding.get("symbol") or holding.get("code") or "")
            items[symbol] = {
                "symbol": symbol, "name": holding.get("name"),
                "sources": ["holding"], "formal_rank": None,
            }
        for symbol, candidate in candidates.items():
            item = items.setdefault(symbol, {
                "symbol": symbol, "name": candidate.get("name"),
                "sources": [], "formal_rank": candidate.get("rank"),
            })
            item["name"] = item.get("name") or candidate.get("name")
            item["formal_rank"] = candidate.get("rank")
            item["sources"].append("formal_top15")
        rows = []
        for symbol, item in items.items():
            candidate = candidates.get(symbol) or {}
            plan = candidate.get("trade_plan") or plans.get(symbol) or {}
            quote = quote_by_symbol.get(symbol) or {}
            freshness = str(quote.get("freshness") or "stale_or_missing")
            close_price = (
                _finite_number(quote.get("price"))
                if freshness in {"actionable", "closing_current"} else None
            )
            entry_low = _finite_number(plan.get("entry_low"))
            entry_high = _finite_number(plan.get("entry_high"))
            blockers = []
            if close_price is None:
                blockers.append("closing_quote_unavailable")
            if not plan or plan.get("available") is False:
                blockers.append("trade_plan_missing")
            rows.append(clean_json(item | {
                "status": "blocked" if blockers else "ready",
                "reference_close": close_price,
                "quote_as_of": quote.get("as_of"),
                "quote_freshness": freshness,
                "buy_zone": (
                    {"low": entry_low, "high": entry_high}
                    if entry_low is not None and entry_high is not None else None
                ),
                "sell_levels": {
                    "stop_loss": _finite_number(plan.get("stop_loss")),
                    "first_target": _finite_number(plan.get("target_price")),
                    "second_target": _finite_number(plan.get("target_price_2")),
                },
                "planned_action": plan.get("action") or "hold",
                "planned_action_cn": plan.get("action_cn") or "不动",
                "plan_reason": str(plan.get("reason") or "")[:300],
                "plan_price_basis": plan.get("price_basis"),
                "blockers": blockers,
            }))
        rows.sort(key=lambda row: (
            0 if "holding" in (row.get("sources") or []) else 1,
            int(row.get("formal_rank") or 9999), str(row.get("symbol") or ""),
        ))
        complete = bool(rows) and all(row.get("status") == "ready" for row in rows)
        return clean_json(base | {
            "status": "complete" if complete else "degraded" if rows else "missing",
            "count": len(rows),
            "rows": rows[:115],
            "price_basis": "same_trading_day_close_plus_persisted_rule_plan",
        })

    @staticmethod
    def _adjustment_proposals(outcomes: dict[str, Any], strategies: dict[str, Any]) -> dict[str, Any]:
        proposals = []
        for row in outcomes.get("buckets") or []:
            directional_n = int(row.get("directional_n") or 0)
            factor = float(row.get("performance_factor") or 1.0)
            deviation = abs(factor - 1.0)
            if (row.get("sample_status") != "evaluated"
                    or directional_n < MIN_STRATEGY_FEEDBACK_SAMPLES
                    or deviation + 1e-9 < MIN_PERFORMANCE_FACTOR_DEVIATION):
                continue
            bounded = max(
                1.0 - MAX_STRATEGY_MULTIPLIER_STEP,
                min(1.0 + MAX_STRATEGY_MULTIPLIER_STEP, factor),
            )
            proposals.append({
                "source_type": row.get("bucket"),
                "direction": "upweight" if bounded > 1.0 else "downweight",
                "observed_performance_factor": factor,
                "proposed_multiplier": round(bounded, 4),
                "directional_samples": directional_n,
                "status": "review_required",
                "rationale": "posterior_feedback_crossed_conservative_threshold",
            })
        blocked_strategies = [
            {
                "strategy_id": row.get("strategy_id"),
                "effective_samples": int(row.get("effective_samples") or 0),
                "blocker": row.get("promotion_blocker") or "independent_evidence_insufficient",
            }
            for row in strategies.get("strategies") or []
            if (row.get("promotion_blocker")
                or int(row.get("effective_samples") or 0) < MIN_STRATEGY_FEEDBACK_SAMPLES)
        ]
        return clean_json({
            "status": "complete" if all(
                item.get("status") == "complete" for item in (outcomes, strategies)
            ) else "degraded",
            "proposal_count": len(proposals),
            "proposals": proposals[:50],
            "blocked_strategy_count": len(blocked_strategies),
            "blocked_strategies": blocked_strategies[:100],
            "guardrails": {
                "minimum_directional_samples": MIN_STRATEGY_FEEDBACK_SAMPLES,
                "minimum_performance_factor_deviation": MIN_PERFORMANCE_FACTOR_DEVIATION,
                "maximum_multiplier_step": MAX_STRATEGY_MULTIPLIER_STEP,
                "human_review_required": True,
                "auto_apply": False,
            },
        })

    def _post_close_review(
        self, now: datetime, trading_day: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        due = (now.hour, now.minute) >= (POST_CLOSE_REVIEW_HOUR, POST_CLOSE_REVIEW_MINUTE)
        base = {
            "due": due,
            "available_after": "20:45 Asia/Shanghai",
            "review_date": now.date().isoformat(),
        }
        if not due:
            pending = base | {
                "status": "pending",
                "conclusion": "盘后闭环尚未到生成时间。",
                "jobs": [],
                "decision_signal_evidence": {"status": "pending", "buckets": []},
                "selection_strategy_evidence": {"status": "pending", "strategies": []},
            }
            proposals = {
                "status": "pending", "proposal_count": 0, "proposals": [],
                "guardrails": {"human_review_required": True, "auto_apply": False},
            }
            return pending, proposals
        if trading_day.get("confirmed") and not trading_day.get("is_trading_day"):
            closed = base | {
                "status": "not_applicable",
                "conclusion": "已确认非交易日，无当日盘后闭环。",
                "jobs": [],
                "decision_signal_evidence": {"status": "not_applicable", "buckets": []},
                "selection_strategy_evidence": {"status": "not_applicable", "strategies": []},
            }
            proposals = {
                "status": "not_applicable", "proposal_count": 0, "proposals": [],
                "guardrails": {"human_review_required": True, "auto_apply": False},
            }
            return closed, proposals

        today = now.date().isoformat()
        try:
            raw_runs = self.job_runs_reader(limit=200) or []
        except Exception:
            raw_runs = []
        latest: dict[str, dict[str, Any]] = {}
        for row in raw_runs:
            if not isinstance(row, dict):
                continue
            job_name = str(row.get("job_name") or "")
            run_day = str(row.get("started_at") or row.get("finished_at") or "")[:10]
            if job_name in POST_CLOSE_JOBS and run_day == today and job_name not in latest:
                latest[job_name] = _job_run(row)
        jobs = [latest.get(name) or {"job_name": name, "status": "missing"}
                for name in POST_CLOSE_JOBS]

        try:
            outcomes = _outcome_evidence(
                self.outcome_stats_reader(
                    dimension="source_type", days=180, ensure_tables=False,
                )
            )
        except Exception:
            outcomes = {"status": "missing", "dimension": "source_type", "buckets": []}
        try:
            strategies = _strategy_evidence(
                self.strategy_evidence_reader(horizon_days=5, lookback_days=180)
            )
        except Exception:
            strategies = {"status": "missing", "strategies": []}
        proposals = self._adjustment_proposals(outcomes, strategies)

        jobs_complete = all(
            row.get("status") in {"success", "not_applicable"} for row in jobs
        )
        evidence_complete = all(
            section.get("status") == "complete" for section in (outcomes, strategies)
        )
        status = "complete" if (
            jobs_complete and evidence_complete and trading_day.get("confirmed")
        ) else "degraded"
        evaluated = sum(
            1 for row in outcomes.get("buckets") or []
            if row.get("sample_status") == "evaluated"
        )
        conclusion = (
            f"盘后闭环完成：后验有效分桶 {evaluated} 个，"
            f"策略调整建议 {proposals.get('proposal_count') or 0} 项，均需人工审核。"
            if status == "complete" else
            "盘后闭环不完整：任务、交易日证据或后验数据存在缺口；不应用策略调整。"
        )
        review = base | {
            "status": status,
            "conclusion": conclusion,
            "jobs": jobs,
            "decision_signal_evidence": outcomes,
            "selection_strategy_evidence": strategies,
        }
        if status != "complete":
            proposals["status"] = "degraded"
        return clean_json(review), clean_json(proposals)

    def read(self, *, owner_id: str) -> dict[str, Any]:
        if not owner_id:
            raise PermissionError("portfolio_scope_required")
        try:
            self._dependencies()
        except Exception:
            snapshot = {
                "schema_version": "scheduled-agent-snapshot-v1",
                "status": "missing",
                "trading_day": self._missing(
                    "runtime_configuration_missing",
                    "Run this read through configured Foliant Agent HTTP; do not use a local empty database.",
                ),
                "cockpit": {"status": "missing"},
                "formal_selection": {"status": "missing", "formal_top15": [], "formal_top5": []},
                "independent_selection": {"status": "missing", "top15": [], "top5": []},
                "wencai_reference": {"status": "missing", "reference_only": True, "strategies": []},
                "external_independent_research": {
                    "status": "missing", "channel": "codex-external-independent-v1",
                    "top15": [], "top5": [], "news_watchlist": [],
                    "tuning_proposals": [], "auto_apply": False, "auto_execution": False,
                },
                "holdings": {"status": "missing", "rows": []},
                "trade_plans": {
                    "status": "missing", "formal": [], "portfolio_risk": {},
                    "formal_candidate_follow_up": [], "next_premarket_check": {"status": "missing"},
                },
                "quotes": {"status": "missing", "rows": []},
                "post_close_review": {"status": "missing", "jobs": []},
                "holdings_review": {"status": "missing", "rows": []},
                "next_session_plan": {"status": "missing", "rows": []},
                "source_comparison": {"status": "missing", "pairwise": {}},
                "strategy_adjustment_proposals": {
                    "status": "missing", "proposal_count": 0, "proposals": [],
                    "guardrails": {"human_review_required": True, "auto_apply": False},
                },
                "as_of": {"captured_at": self.clock().isoformat(timespec="seconds")},
                "quality": {"status": "missing", "error_code": "runtime_configuration_missing"},
            }
            return tool_result(
                summary="Scheduled snapshot is unavailable because the Foliant runtime is not configured.",
                resource_uri="shadow://foliant/reports/scheduled-snapshot-missing",
                status="missing",
                provenance_value=provenance(run_id="scheduled-snapshot-missing"),
                warnings=["runtime configuration is missing"], data=snapshot,
                model_payload=snapshot,
            )

        now = self.clock()
        today = now.date().isoformat()
        trading_day = self._trading_day(today)
        phase = _report_phase(now, trading_day)
        cockpit = self._cockpit()
        cockpit["raw_status"] = cockpit.get("status")
        try:
            selection_value = self.selection_reader() or {}
        except Exception:
            selection_value = {"status": "missing", "data": None, "warnings": []}
        formal = self._formal(selection_value, trading_day)
        wencai = self._wencai(selection_value)
        independent = self._independent(
            selection_value, expected_market_as_of=formal.get("market_as_of"),
        )
        source_comparison = self._source_comparison(
            selection_value, formal, independent, wencai,
        )
        try:
            external_raw = self.external_research_reader() or {}
        except Exception:
            external_raw = {"status": "missing"}
        external_research = self._external_research(
            external_raw, formal=formal, independent=independent, wencai=wencai,
        )
        source_comparison["availability"]["external_independent"] = (
            external_research.get("status") == "complete"
        )
        source_comparison["external_top5"] = external_research.get("comparison")

        try:
            context = self.context_reader() or {"holdings": [], "watermark": ""}
            holding_rows = [_holding(row) for row in (context.get("holdings") or [])
                            if float(row.get("quantity") or 0) > 0]
            holdings = {
                "status": "complete", "portfolio_ref": "primary",
                "count": len(holding_rows), "rows": holding_rows[:100],
                "as_of": now.isoformat(timespec="seconds"),
                "authoritative_source": "portfolio_db.action_preview_context",
            }
        except Exception:
            context = None
            holding_rows = []
            holdings = self._missing(
                "portfolio_unavailable", "Verify the portfolio-primary grant and PostgreSQL runtime.",
            ) | {"rows": []}

        try:
            capsule = self.capsule_reader()
        except Exception:
            capsule = None

        extra = [row.get("symbol") for row in formal.get("formal_top15") or []]
        symbols = account_quote_symbols(capsule or {}, holding_rows, extra_symbols=extra)
        raw_quotes: dict[str, Any] = {}
        quote_state = "complete"
        if symbols:
            try:
                raw_quotes = self.quote_loader(symbols) or {}
                if len(raw_quotes) < len(symbols):
                    quote_state = "degraded"
            except Exception:
                quote_state = "missing"
        pricing_at = self.clock()
        if pricing_at.tzinfo is None:
            pricing_at = pricing_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        pricing_at = pricing_at.astimezone(ZoneInfo("Asia/Shanghai"))
        quote_rows = [_quote(symbol, raw_quotes.get(symbol)) for symbol in symbols]
        quote_mode = (
            "post_close"
            if trading_day.get("confirmed") and trading_day.get("is_trading_day")
            and (now.hour, now.minute) >= (15, 0)
            else "intraday"
        )
        try:
            from jobs.intraday_decision_monitor import assess_quotes

            quote_quality = assess_quotes(
                [{"symbol": symbol} for symbol in symbols], raw_quotes, pricing_at,
                mode=quote_mode,
            )
            by_symbol = quote_quality.get("items") or {}
            for row in quote_rows:
                assessed = by_symbol.get(row["symbol"]) or {}
                row["as_of"] = assessed.get("quote_as_of") or row.get("as_of")
                row["freshness"] = assessed.get("freshness") or "stale_or_missing"
            quote_state = quote_quality.get("status") or quote_state
        except Exception:
            quote_quality = {}
        quotes = {
            "status": quote_state,
            "requested_count": len(symbols),
            "available_count": sum(1 for row in quote_rows if row.get("price") is not None),
            "batch_count": 1 if symbols else 0,
            "rows": quote_rows[:115],
            "as_of": max((str(row.get("as_of") or "") for row in quote_rows), default="") or None,
            "missing_by_asset_type": quote_quality.get("missing_by_asset_type") or {},
            "unsupported_asset_symbols": quote_quality.get("unsupported_asset_symbols") or [],
        }
        quotes["missing_symbols"] = [
            row["symbol"] for row in quote_rows if row.get("price") is None
        ]
        quotes["missing_count"] = len(quotes["missing_symbols"])
        cockpit["optional_provider_degradations"] = (
            _optional_quote_provider_degradations(cockpit, quotes)
        )
        cockpit["phase_quality_status"] = _cockpit_quality_for_phase(
            cockpit, phase, quotes,
        )
        if cockpit["phase_quality_status"] != cockpit.get("status"):
            reasons = set(cockpit.get("degradation_reasons") or [])
            cockpit["expected_phase_degradations"] = (
                ["auxiliary_quote_provider_failed_with_complete_fallback"]
                if reasons == {"optional_quote_provider_degraded"} else
                ["intraday_market_add_signal_expired_after_close"]
            )

        try:
            cash_fact = self.cash_reader() or {}
        except Exception:
            cash_fact = {"status": "missing", "reason": "cash_reader_unavailable"}
        try:
            security_metadata = self.security_metadata_reader(today) or {}
        except Exception:
            security_metadata = {
                "stock_symbols": set(), "fund_symbols": set(),
                "sources": [], "errors": ["security_metadata_reader_unavailable"],
            }
        stock_budget = derive_stock_budget(
            holding_rows, quote_rows, security_metadata,
        )
        available_cash = stock_budget.get("available_cash_cny")
        preview_context = context
        if context is not None and stock_budget.get("status") == "complete":
            classifications = stock_budget.get("classifications") or {}
            preview_context = {
                **context,
                "holdings": [
                    row for row in (context.get("holdings") or [])
                    if classifications.get(
                        str(row.get("code") or row.get("symbol") or "").zfill(6)
                    ) == "stock"
                ],
            }

        if context is None:
            account_plan = {"status": "missing", "preview_only": True,
                            "error_code": "portfolio_unavailable"}
        elif not capsule:
            account_plan = {"status": "missing", "preview_only": True,
                            "error_code": "formal_capsule_missing"}
        else:
            try:
                refreshed = self.context_reader() or {}
                if refreshed.get("watermark") != context.get("watermark"):
                    account_plan = {"status": "stale", "preview_only": True,
                                    "error_code": "holdings_changed_during_preview",
                                    "alternatives": []}
                else:
                    account_plan = build_account_preview(
                        owner_id=owner_id, capsule=capsule, context=preview_context,
                        raw_quotes=raw_quotes, available_cash=available_cash,
                        allow_add=False, now=pricing_at,
                        cash_basis=(
                            STOCK_BUDGET_BASIS if available_cash is not None else None
                        ),
                        quote_ttl_seconds=int(
                            float(quote_quality.get("stale_minutes") or 8) * 60
                        ),
                        quote_freshness_by_symbol={
                            row["symbol"]: row.get("freshness") for row in quote_rows
                        },
                        pricing_mode=quote_mode,
                    )
            except Exception:
                account_plan = {"status": "degraded", "preview_only": True,
                                "error_code": "portfolio_risk_unavailable"}

        pricing_snapshot = (
            account_plan.get("pricing_snapshot")
            if isinstance(account_plan, dict) else None
        ) or {}
        quotes["snapshot_id"] = pricing_snapshot.get("snapshot_id")
        quotes["oldest_usable_as_of"] = quote_quality.get("quote_as_of")
        quotes["quote_ttl_seconds"] = pricing_snapshot.get("quote_ttl_seconds")
        quotes["latest_as_of"] = quotes.get("as_of")
        quotes["as_of"] = pricing_snapshot.get("oldest_as_of") or quotes.get("as_of")
        quotes["captured_at"] = pricing_at.isoformat(timespec="seconds")
        closing_pricing_snapshot = clean_json({
            "snapshot_id": quotes.get("snapshot_id"),
            "as_of": quotes.get("as_of"),
            "latest_as_of": quotes.get("latest_as_of"),
            "captured_at": quotes.get("captured_at"),
            "mode": quote_mode,
        })

        try:
            intraday_value = self.intraday_reader() or {}
            intraday = intraday_value.get("data") or {}
        except Exception:
            intraday = {}
        intraday_quote_binding = "persisted_reference"
        if phase == "intraday" and context is not None and account_plan.get("status") not in {
            "missing", "stale",
        }:
            projected_intraday = self._project_intraday_from_loaded_facts(
                now=pricing_at, context=context, raw_quotes=raw_quotes, previous=intraday,
            )
            if (projected_intraday.get("trade_date") == today
                    and projected_intraday.get("status") not in {"error", "skipped"}
                    and str(projected_intraday.get("selection_run_id") or "")
                    == str(formal.get("run_id") or "")):
                intraday = projected_intraday
                intraday_quote_binding = "same_snapshot_quote_batch"
        plan_run_matches = bool(
            formal.get("run_id")
            and str(intraday.get("selection_run_id") or "") == str(formal.get("run_id"))
        )
        persisted_plans = (intraday.get("plans") or {}) if plan_run_matches else {}
        review_plans = {
            str(symbol): dict(plan) for symbol, plan in persisted_plans.items()
            if isinstance(plan, dict)
        }
        for candidate in formal.get("formal_top15") or []:
            symbol = str(candidate.get("symbol") or "")
            if isinstance(candidate.get("trade_plan"), dict):
                review_plans[symbol] = dict(candidate["trade_plan"])
        formal_plans = [
            {"symbol": row.get("symbol"),
             "trade_plan": row.get("trade_plan") or persisted_plans.get(row.get("symbol"))}
            for row in formal.get("formal_top15") or []
            if row.get("trade_plan") or persisted_plans.get(row.get("symbol"))
        ]
        plan_projection = _action_plan(account_plan)
        raw_plan_blockers = list(plan_projection.get("blockers") or [])
        buy_blockers = [item for item in raw_plan_blockers if item == "cash_unknown"]
        operational_blockers = [item for item in raw_plan_blockers if item != "cash_unknown"]
        plan_projection["blockers"] = operational_blockers
        plan_projection["buy_blockers"] = buy_blockers
        plan_projection["sell_blockers"] = operational_blockers
        plan_state = str(plan_projection.get("status") or "complete")
        if plan_projection.get("error_code") or operational_blockers:
            plan_state = "degraded" if plan_state not in {"missing", "stale"} else plan_state
        candidate_follow_up = self._candidate_follow_up(
            formal, quote_rows, trade_plans=review_plans,
            stock_budget=stock_budget, phase=phase,
        )
        trade_plans = {
            "status": (
                plan_state
                if phase == "intraday" and plan_state in {"missing", "stale"} else
                "degraded" if phase == "intraday"
                and intraday_quote_binding != "same_snapshot_quote_batch" else
                plan_state if phase == "intraday" else "pending"
            ),
            "phase": phase,
            "formal": formal_plans[:15],
            "formal_candidate_follow_up": candidate_follow_up,
            "next_premarket_check": self._next_premarket_check(formal, independent=independent),
            "portfolio_risk": plan_projection,
            "holding_actions": clean_json(intraday.get("holdings") or [])[:100],
            "portfolio_action_guard": clean_json(
                intraday.get("portfolio_action_guard") or {}
            ),
            "holding_actions_authority": {
                "status": (
                    "current" if phase == "intraday"
                    and intraday_quote_binding == "same_snapshot_quote_batch" else
                    "stale_or_missing" if phase == "intraday" else
                    "historical_reference"
                ),
                "as_of": intraday.get("generated_at"),
                "quote_binding": intraday_quote_binding,
                "pricing_snapshot_id": quotes.get("snapshot_id"),
                "reason": (
                    "same_request_quotes_then_intraday_decisions"
                    if phase == "intraday"
                    and intraday_quote_binding == "same_snapshot_quote_batch" else
                    "persisted_intraday_decisions_not_bound_to_current_quotes"
                    if phase == "intraday" else
                    "intraday_decisions_are_not_authoritative_after_close"
                ),
            },
            "intraday_as_of": intraday.get("generated_at"),
            "intraday_plan_binding": {
                "status": (
                    "current" if plan_run_matches and phase == "intraday" else
                    "historical_reference" if plan_run_matches else "stale_or_missing"
                ),
                "selection_run_id": intraday.get("selection_run_id"),
                "expected_run_id": formal.get("run_id"),
                "quote_binding": intraday_quote_binding,
                "pricing_snapshot_id": quotes.get("snapshot_id"),
            },
            "cash_policy": {
                "cash_basis": STOCK_BUDGET_BASIS,
                "cash_status": stock_budget.get("status") or "blocked",
                "cash_as_of": stock_budget.get("as_of"),
                "stock_budget": stock_budget,
                "new_or_add_positions_allowed": False,
                "conservative_reductions_allowed": True,
                "buy_side": {
                    "status": "preview_only" if available_cash is not None else "blocked",
                    "blockers": list(stock_budget.get("buy_side_blockers") or []),
                },
                "sell_side": {
                    "status": "available",
                    "blockers": operational_blockers,
                },
                "affects_snapshot_quality": False,
                "broker_cash_source": "unavailable",
                "broker_cash_balance": False,
                "user_declared_total_budget_cny": stock_budget.get("total_budget_cny"),
                "reason": (
                    "scheduled_preview_never_enables_additions"
                    if available_cash is not None else
                    "cash_unknown_blocks_additions_but_not_risk_reduction_preview"
                ),
            },
            "decision_boundary": {
                "status": (
                    "pricing_only"
                    if cockpit.get("blocking_dimensions") else "preview_only"
                ),
                "blocking_dimensions": [
                    row.get("dimension")
                    for row in cockpit.get("blocking_dimensions") or []
                ],
                "blocked_actions": sorted({
                    action
                    for row in cockpit.get("blocking_dimensions") or []
                    for action in (row.get("affected_decisions") or [])
                }),
                "auto_execution": False,
            },
            "source_contracts": {
                "position_truth": {
                    "source": "portfolio_db.action_preview_context",
                    "authoritative_for": "holdings_and_portfolio_watermark",
                },
                "portfolio_snapshot": {
                    "source": "portfolio.portfolio_snapshot",
                    "authoritative_for": "historical_portfolio_nav",
                    "invoked": False,
                },
                "position_guardian": {
                    "source": "portfolio.position_guardian",
                    "authoritative_for": "add_trigger_review",
                    "invoked": False,
                },
                "exit_advisor": {
                    "source": "portfolio.exit_advisor",
                    "authoritative_for": "exit_priority_review",
                    "invoked": False,
                },
                "current_holding_actions": {
                    "source": (
                        "jobs.intraday_decision_monitor.same_quote_batch_projection"
                        if intraday_quote_binding == "same_snapshot_quote_batch" else
                        "jobs.intraday_decision_monitor.latest_snapshot"
                    ),
                    "authoritative_for": (
                        "current_read_only_intraday_decisions"
                        if intraday_quote_binding == "same_snapshot_quote_batch" else
                        "persisted_intraday_reference"
                    ),
                    "quote_binding": intraday_quote_binding,
                },
                "cash_balance": {
                    "source": "application.stock_budget.derive_stock_budget",
                    "authoritative_for": "stock_budget_available_cash_preview_only",
                    "basis": STOCK_BUDGET_BASIS,
                    "status": stock_budget.get("status") or "blocked",
                    "broker_cash_balance": False,
                    "legacy_confirmed_cash_fact_status": cash_fact.get("status") or "missing",
                },
            },
            "preview_only": True,
            "auto_execution": False,
            "pricing_status": quote_quality.get("status") or quote_state,
            "pricing_context": quote_quality.get("mode") or "intraday",
            "current_authority": (
                "intraday_rule_plan" if phase == "intraday" else
                "pending_post_close_review"
            ),
        }

        post_close_review, adjustment_proposals = self._post_close_review(now, trading_day)
        review_due = bool(post_close_review.get("due"))
        holdings_review = self._holdings_review(
            due=review_due, trading_day=trading_day, holdings=holding_rows,
            quote_rows=quote_rows, plans=review_plans,
            pricing_snapshot=closing_pricing_snapshot,
        )
        next_session_plan = self._next_session_plan(
            due=review_due, trading_day=trading_day, formal=formal,
            holdings=holding_rows, quote_rows=quote_rows, plans=review_plans,
            pricing_snapshot=closing_pricing_snapshot,
        )
        post_close_review["holdings_review_status"] = holdings_review.get("status")
        post_close_review["next_session_plan_status"] = next_session_plan.get("status")
        post_close_review["source_comparison_status"] = source_comparison.get("status")
        if phase == "post_close_review":
            trade_plans["status"] = next_session_plan.get("status") or "degraded"
            trade_plans["current_authority"] = "next_session_plan"

        section_status = {
            "trading_day": trading_day.get("status"),
            "cockpit": cockpit.get("phase_quality_status") or cockpit.get("status"),
            "formal_selection": formal.get("status"),
            "independent_selection": independent.get("status"),
            "wencai_reference": wencai.get("status"),
            "external_independent_research": external_research.get("status"),
            "holdings": holdings.get("status"),
            "trade_plans": trade_plans.get("status"),
            "quotes": quotes.get("status"),
            "post_close_review": post_close_review.get("status"),
            "holdings_review": holdings_review.get("status"),
            "next_session_plan": next_session_plan.get("status"),
            "source_comparison": source_comparison.get("status"),
            "strategy_adjustment_proposals": adjustment_proposals.get("status"),
        }
        optional_sections = ("wencai_reference", "external_independent_research")
        pending_allowed = {
            "post_close_review", "holdings_review", "next_session_plan",
            "strategy_adjustment_proposals",
        }
        if phase == "post_close_pending":
            pending_allowed.add("trade_plans")
        healthy_states = {"complete", "success", "not_applicable"}
        blocking_sections = [
            name for name, value in section_status.items()
            if name not in optional_sections
            and value not in healthy_states
            and not (value == "pending" and name in pending_allowed)
        ]
        healthy = not blocking_sections
        captured_at = pricing_at.isoformat(timespec="seconds")
        snapshot = {
            "schema_version": "scheduled-agent-snapshot-v1",
            "status": "complete" if healthy else "degraded",
            "phase": phase,
            "trading_day": trading_day,
            "cockpit": cockpit,
            "formal_selection": formal,
            "independent_selection": independent,
            "wencai_reference": wencai,
            "external_independent_research": external_research,
            "holdings": holdings,
            "trade_plans": trade_plans,
            "quotes": quotes,
            "post_close_review": post_close_review,
            "holdings_review": holdings_review,
            "next_session_plan": next_session_plan,
            "source_comparison": source_comparison,
            "strategy_adjustment_proposals": adjustment_proposals,
            "as_of": {
                "captured_at": captured_at,
                "calendar": trading_day.get("as_of"),
                "formal_selection": formal.get("selection_date"),
                "independent_selection": independent.get("market_as_of"),
                "wencai": wencai.get("as_of"),
                "external_independent_research": external_research.get("decision_as_of"),
                "holdings": holdings.get("as_of"),
                "quotes": quotes.get("as_of"),
                "post_close_review": (
                    captured_at if post_close_review.get("due") else None
                ),
            },
            "quality": {
                "status": "complete" if healthy else "degraded",
                "phase": phase,
                "sections": section_status,
                "blocking_sections": blocking_sections,
                "optional_degradations": [
                    name for name in optional_sections
                    if section_status.get(name) not in healthy_states
                ],
                "optional_source_degradations": [
                    row.get("source")
                    for row in cockpit.get("optional_provider_degradations") or []
                ],
                "pending_is_normal": phase == "post_close_pending",
            },
        }
        snapshot = _redact(snapshot)
        snapshot_id = payload_hash(snapshot)
        return tool_result(
            summary=("Scheduled Foliant snapshot is complete."
                     if healthy else "Scheduled Foliant snapshot is degraded; inspect section quality."),
            resource_uri=f"shadow://foliant/reports/scheduled-{snapshot_id[:24]}",
            status="complete" if healthy else "degraded",
            provenance_value=provenance(
                run_id=f"scheduled-{snapshot_id[:16]}", decision_at=captured_at,
                market_as_of=formal.get("selection_date"),
                input_manifest_id=(selection_value.get("provenance") or {}).get("input_manifest_id"),
                policy_hash=(selection_value.get("provenance") or {}).get("policy_hash"),
                code_revision=(selection_value.get("provenance") or {}).get("code_revision"),
            ),
            warnings=blocking_sections,
            data=snapshot,
            model_payload=snapshot,
        )
