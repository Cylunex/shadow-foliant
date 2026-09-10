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
    return state if state in {"complete", "success", "stale", "missing", "degraded", "partial"} else default


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.selection_reader = selection_reader
        self.cockpit_reader = cockpit_reader
        self.context_reader = context_reader
        self.capsule_reader = capsule_reader
        self.intraday_reader = intraday_reader
        self.quote_loader = quote_loader
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
    def _independent(selection_value: dict[str, Any]) -> dict[str, Any]:
        data = selection_value.get("data") or {}
        payload = ((data.get("references") or {}).get("independent") or {})
        ready = payload.get("status") == "ready"
        return {
            "status": "complete" if ready else "missing",
            "availability": payload.get("status") or "unavailable",
            "reason": payload.get("reason"),
            "strategy_id": payload.get("strategy_id"),
            "strategy_version": payload.get("strategy_version"),
            "strategy_hash": payload.get("strategy_hash"),
            "manifest_id": payload.get("manifest_id"),
            "input_snapshot_id": payload.get("input_snapshot_id"),
            "input_provenance": clean_json(payload.get("input_provenance") or {}),
            "market_as_of": payload.get("market_as_of"),
            "weights": clean_json(payload.get("weights") or {}),
            "top15": [_candidate(row) for row in (payload.get("top15") or [])][:15],
            "top5": [_candidate(row) for row in (payload.get("top5") or [])][:5],
            "independence_boundary": payload.get("independence_boundary"),
            "comparison": clean_json(data.get("selection_comparison") or {}),
        }

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
                "trade_plans": {"status": "missing", "formal": [], "portfolio_risk": {}},
                "quotes": {"status": "missing", "rows": []},
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
        independent = self._independent(selection_value)

        try:
            context = self.context_reader() or {"holdings": [], "watermark": ""}
            holding_rows = [_holding(row) for row in (context.get("holdings") or [])
                            if float(row.get("quantity") or 0) > 0]
            holdings = {
                "status": "complete", "portfolio_ref": "primary",
                "count": len(holding_rows), "rows": holding_rows[:100],
                "as_of": now.isoformat(timespec="seconds"),
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
        trade_plans = {
            "status": plan_state,
            "formal": formal_plans[:15],
            "portfolio_risk": plan_projection,
            "holding_actions": clean_json(intraday.get("holdings") or [])[:100],
            "intraday_as_of": intraday.get("generated_at"),
            "preview_only": True,
            "auto_execution": False,
        }

        section_status = {
            "trading_day": trading_day.get("status"),
            "cockpit": cockpit.get("status"),
            "formal_selection": formal.get("status"),
            "independent_selection": independent.get("status"),
            "wencai_reference": wencai.get("status"),
            "holdings": holdings.get("status"),
            "trade_plans": trade_plans.get("status"),
            "quotes": quotes.get("status"),
        }
        healthy = all(value in {"complete", "success"} for value in section_status.values())
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
            "as_of": {
                "captured_at": captured_at,
                "calendar": trading_day.get("as_of"),
                "formal_selection": formal.get("selection_date"),
                "independent_selection": independent.get("market_as_of"),
                "wencai": wencai.get("as_of"),
                "holdings": holdings.get("as_of"),
                "quotes": quotes.get("as_of"),
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
                      if value not in {"complete", "success"}],
            data=snapshot,
            model_payload=snapshot,
        )
