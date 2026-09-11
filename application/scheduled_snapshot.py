"""Bounded, read-only snapshot for repository-external scheduled consumers."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from application.account_preview import (
    account_quote_symbols,
    build_account_preview,
)
from application.results import clean_json, payload_hash, provenance, tool_result


EXPECTED_WENCAI_STRATEGIES = (
    "主力资金", "低价擒牛", "小市值", "净利增长", "低估值",
)
POST_CLOSE_REVIEW_HOUR = 20
POST_CLOSE_REVIEW_MINUTE = 45
POST_CLOSE_JOBS = ("eod_outcomes", "daily_backtest")
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
    )
    return clean_json({key: value.get(key) for key in allowed if key in value})


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
    })


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
        quote_loader: Callable[[list[str]], dict[str, Any]] | None = None,
        job_runs_reader: Callable[..., list[dict[str, Any]]] | None = None,
        outcome_stats_reader: Callable[..., dict[str, Any]] | None = None,
        strategy_evidence_reader: Callable[..., dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.selection_reader = selection_reader
        self.cockpit_reader = cockpit_reader
        self.context_reader = context_reader
        self.capsule_reader = capsule_reader
        self.intraday_reader = intraday_reader
        self.quote_loader = quote_loader
        self.job_runs_reader = job_runs_reader
        self.outcome_stats_reader = outcome_stats_reader
        self.strategy_evidence_reader = strategy_evidence_reader
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))

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
                "picks": picks[:15],
            })
        present = sum(1 for row in rows if row["status"] != "missing")
        ready = sum(1 for row in rows if row["status"] == "ready")
        return {
            "status": "missing" if present == 0 else "complete" if ready == 5 else "degraded",
            "reference_only": True,
            "reference_affects_membership": False,
            "expected_groups": 5,
            "present_groups": present,
            "ready_groups": ready,
            "strategies": rows,
            "as_of": payload.get("executed_at"),
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
    ) -> list[dict[str, Any]]:
        quote_by_symbol = {str(row.get("symbol") or ""): row for row in quote_rows}
        result = []
        for candidate in formal.get("formal_top15") or []:
            symbol = str(candidate.get("symbol") or "")
            quote = quote_by_symbol.get(symbol) or {}
            trade_plan = candidate.get("trade_plan") or {}
            plan_available = bool(trade_plan) and trade_plan.get("available") is not False
            blockers = []
            if formal.get("status") not in {"complete", "success"}:
                blockers.append("formal_selection_not_current")
            if quote.get("freshness") != "actionable":
                blockers.append("quote_stale_or_missing")
            if not plan_available:
                blockers.append("trade_plan_missing")
            result.append(clean_json({
                "symbol": symbol,
                "name": candidate.get("name"),
                "rank": candidate.get("rank"),
                "status": "blocked" if blockers else "ready",
                "quote_as_of": quote.get("as_of"),
                "quote_freshness": quote.get("freshness") or "stale_or_missing",
                "trade_plan_available": plan_available,
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

        jobs_complete = all(row.get("status") == "success" for row in jobs)
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
                "holdings": {"status": "missing", "rows": []},
                "trade_plans": {
                    "status": "missing", "formal": [], "portfolio_risk": {},
                    "formal_candidate_follow_up": [], "next_premarket_check": {"status": "missing"},
                },
                "quotes": {"status": "missing", "rows": []},
                "post_close_review": {"status": "missing", "jobs": []},
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
        cockpit = self._cockpit()
        try:
            selection_value = self.selection_reader() or {}
        except Exception:
            selection_value = {"status": "missing", "data": None, "warnings": []}
        formal = self._formal(selection_value, trading_day)
        wencai = self._wencai(selection_value)
        independent = self._independent(
            selection_value, expected_market_as_of=formal.get("selection_date"),
        )

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
        quote_rows = [_quote(symbol, raw_quotes.get(symbol)) for symbol in symbols]
        try:
            from jobs.intraday_decision_monitor import assess_quotes

            quote_quality = assess_quotes(
                [{"symbol": symbol} for symbol in symbols], raw_quotes, now
            )
            by_symbol = quote_quality.get("items") or {}
            for row in quote_rows:
                assessed = by_symbol.get(row["symbol"]) or {}
                row["as_of"] = assessed.get("quote_as_of") or row.get("as_of")
                row["freshness"] = (
                    "actionable" if assessed.get("price_actionable") else "stale_or_missing"
                )
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
                        owner_id=owner_id, capsule=capsule, context=context,
                        raw_quotes=raw_quotes, available_cash=None, allow_add=False, now=now,
                    )
            except Exception:
                account_plan = {"status": "degraded", "preview_only": True,
                                "error_code": "portfolio_risk_unavailable"}

        try:
            intraday_value = self.intraday_reader() or {}
            intraday = intraday_value.get("data") or {}
        except Exception:
            intraday = {}
        persisted_plans = intraday.get("plans") or {}
        formal_plans = [
            {"symbol": row.get("symbol"),
             "trade_plan": row.get("trade_plan") or persisted_plans.get(row.get("symbol"))}
            for row in formal.get("formal_top15") or []
            if row.get("trade_plan") or persisted_plans.get(row.get("symbol"))
        ]
        plan_projection = _action_plan(account_plan)
        plan_state = str(plan_projection.get("status") or "complete")
        if plan_projection.get("error_code") or plan_projection.get("blockers"):
            plan_state = "degraded" if plan_state not in {"missing", "stale"} else plan_state
        candidate_follow_up = self._candidate_follow_up(formal, quote_rows)
        trade_plans = {
            "status": plan_state,
            "formal": formal_plans[:15],
            "formal_candidate_follow_up": candidate_follow_up,
            "next_premarket_check": self._next_premarket_check(formal, independent=independent),
            "portfolio_risk": plan_projection,
            "holding_actions": clean_json(intraday.get("holdings") or [])[:100],
            "intraday_as_of": intraday.get("generated_at"),
            "cash_policy": {
                "cash_basis": "unknown_unverified",
                "new_or_add_positions_allowed": False,
                "conservative_reductions_allowed": True,
                "reason": "cash_unknown_blocks_additions_but_not_risk_reduction_preview",
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
                    "source": "jobs.intraday_decision_monitor.latest_snapshot",
                    "authoritative_for": "persisted_intraday_decisions",
                },
            },
            "preview_only": True,
            "auto_execution": False,
        }

        post_close_review, adjustment_proposals = self._post_close_review(now, trading_day)

        section_status = {
            "trading_day": trading_day.get("status"),
            "cockpit": cockpit.get("status"),
            "formal_selection": formal.get("status"),
            "independent_selection": independent.get("status"),
            "wencai_reference": wencai.get("status"),
            "holdings": holdings.get("status"),
            "trade_plans": trade_plans.get("status"),
            "quotes": quotes.get("status"),
            "post_close_review": post_close_review.get("status"),
            "strategy_adjustment_proposals": adjustment_proposals.get("status"),
        }
        healthy_states = {"complete", "success", "pending", "not_applicable"}
        healthy = all(value in healthy_states for value in section_status.values())
        captured_at = now.isoformat(timespec="seconds")
        snapshot = {
            "schema_version": "scheduled-agent-snapshot-v1",
            "status": "complete" if healthy else "degraded",
            "trading_day": trading_day,
            "cockpit": cockpit,
            "formal_selection": formal,
            "independent_selection": independent,
            "wencai_reference": wencai,
            "holdings": holdings,
            "trade_plans": trade_plans,
            "quotes": quotes,
            "post_close_review": post_close_review,
            "strategy_adjustment_proposals": adjustment_proposals,
            "as_of": {
                "captured_at": captured_at,
                "calendar": trading_day.get("as_of"),
                "formal_selection": formal.get("selection_date"),
                "independent_selection": independent.get("market_as_of"),
                "wencai": wencai.get("as_of"),
                "holdings": holdings.get("as_of"),
                "quotes": quotes.get("as_of"),
                "post_close_review": (
                    captured_at if post_close_review.get("due") else None
                ),
            },
            "quality": {"status": "complete" if healthy else "degraded",
                        "sections": section_status},
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
            warnings=[name for name, value in section_status.items()
                      if value not in healthy_states],
            data=snapshot,
            model_payload=snapshot,
        )
