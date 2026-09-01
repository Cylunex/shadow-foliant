"""Runtime-neutral Foliant application services.

Transport adapters are intentionally absent.  Services validate use-case inputs, preserve formal
versus preview semantics and return stable domain-shaped results.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from concurrent.futures import Executor
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401
from application.results import (
    bounded_model_payload,
    clean_json,
    now_iso,
    payload_hash,
    provenance,
    stable_failure,
    tool_result,
)
from application.run_repository import (
    IdempotencyConflict,
    RunQuotaExceeded,
    RunRepository,
)
from core.decision_context import code_revision


class ApplicationError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_symbol(value: Any) -> str:
    digits = str(value or "").strip()
    if re.fullmatch(r"[0-9]{6}", digits) is None:
        raise ApplicationError("invalid_symbol", "symbol must be a 6 digit A-share code")
    return digits


def _bounded_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bounded_float(value: Any, *, minimum: float, maximum: float,
                   default: float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ApplicationError(
            f"invalid_{field}", f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def _public_run(run: Mapping[str, Any], *, include_result: bool = False) -> dict[str, Any]:
    result = {
        "run_id": str(run.get("run_id") or ""),
        "status": str(run.get("status") or "failed"),
        "mode": str(run.get("mode") or "preview"),
        "kind": str(run.get("run_kind") or ""),
        "resource_uri": str(run.get("resource_uri") or ""),
        "run_resource_uri": f"shadow://foliant/runs/{run.get('run_id')}",
        "summary": str(run.get("summary") or ""),
        "provenance": clean_json(run.get("provenance") or {}),
        "warnings": clean_json(run.get("warnings") or []),
        "error": str(run.get("error_code") or "") or None,
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "failed_at": run.get("failed_at"),
        "cancellable": bool(run.get("cancellable")),
        "cancellation_note": (
            "the research Profile does not expose cancellation; previews never publish formal state"
            if run.get("status") in {"queued", "running"}
            else "the Run is already terminal"
        ),
    }
    if include_result:
        result["result"] = clean_json(run.get("result_payload"))
    return result


class RunCoordinator:
    """Persist before execution and converge every background job to a stable terminal state."""

    def __init__(self, repository: RunRepository, *, executor: Executor | None = None,
                 max_active_per_actor: int | None = None, recover: bool = False) -> None:
        self.repository = repository
        # Production only persists Runs here. The jobs-hub owns durable execution.
        # An injected executor remains as a deterministic unit-test seam.
        self.executor = executor
        self.max_active_per_actor = max_active_per_actor or _bounded_int(
            os.getenv("FOLIANT_AGENT_ACTIVE_RUN_LIMIT"), minimum=1, maximum=10, default=2
        )
        if recover:
            self.repository.recover_incomplete()

    def submit(
        self,
        *,
        actor_id: str,
        capability: str,
        run_kind: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        request_id: str,
        resource_uri_factory: Callable[[str], str],
        event_type: str,
        runner: Callable[[str], dict[str, Any]],
        run_timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if len(key) < 8 or len(key) > 160:
            raise ApplicationError(
                "idempotency_key_required",
                "Idempotency-Key must contain 8 to 160 characters",
                status_code=400,
            )
        existing = self.repository.get_by_idempotency(actor_id, capability, key)
        request_digest = payload_hash(request_payload)
        if existing:
            if existing.get("request_hash") != request_digest:
                raise ApplicationError(
                    "idempotency_conflict",
                    "Idempotency-Key was already used for a different request",
                    status_code=409,
                )
            return _public_run(existing)
        try:
            created = self.repository.create_or_get(
                actor_id=actor_id,
                capability=capability,
                run_kind=run_kind,
                idempotency_key=key,
                request_payload=request_payload,
                request_id=request_id,
                resource_uri_factory=resource_uri_factory,
                max_active=self.max_active_per_actor,
                timeout_seconds=max(30, min(7200, int(run_timeout_seconds))),
            )
        except IdempotencyConflict as exc:
            raise ApplicationError("idempotency_conflict", str(exc), status_code=409) from exc
        except RunQuotaExceeded as exc:
            raise ApplicationError("run_quota_exceeded", str(exc), status_code=429) from exc
        if created.created and self.executor is not None:
            self.executor.submit(
                self._execute, created.run["run_id"], event_type, runner,
                max(30, min(7200, int(run_timeout_seconds))),
            )
        return _public_run(created.run)

    def _execute(self, run_id: str, event_type: str,
                 runner: Callable[[str], dict[str, Any]], timeout_seconds: int) -> None:
        if not self.repository.mark_running(run_id):
            return
        try:
            result = runner(run_id)
            self.repository.complete(run_id, result, event_type=event_type)
        except Exception as exc:  # noqa: BLE001 - every Run must reach a stable terminal state
            self.repository.fail(run_id, stable_failure(exc))


class MarketOverviewService:
    def read(self) -> dict[str, Any]:
        from analysis.local_stock_selector import SelectionPolicy
        from data.research_store import ResearchStore

        store = ResearchStore(ensure_schema=False)
        policy = SelectionPolicy.from_env()
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        confirmed = store.expected_market_as_of(today, inclusive=True)
        warnings: list[str] = []
        indices: list[Any] = []
        try:
            import datahub

            indices = clean_json(datahub.indices() or [])
        except Exception:  # noqa: BLE001 - live indices are an optional degradation
            warnings.append("live index snapshot is unavailable")
        is_weekday = now.weekday() < 5
        is_trading_hours = is_weekday and (9 <= now.hour < 15)
        snapshot_id = payload_hash({"date": today, "confirmed": confirmed, "indices": indices})
        prov = provenance(
            run_id=f"market-{snapshot_id[:16]}",
            decision_at=now.isoformat(timespec="seconds"),
            market_as_of=confirmed,
            financial_cutoff_at=now.isoformat(timespec="seconds"),
            universe_snapshot_id=None,
            input_manifest_id=snapshot_id,
            policy_hash=policy.policy_hash,
            code_revision=code_revision(),
        )
        data = {
            "calendar_date": today,
            "latest_confirmed_trade_date": confirmed,
            "market_session": "trading" if is_trading_hours else "closed",
            "indices": indices,
        }
        return tool_result(
            summary=(f"A-share market is {data['market_session']}; latest confirmed trade date "
                     f"is {confirmed or 'unavailable'}."),
            resource_uri=f"shadow://foliant/reports/market-{snapshot_id[:24]}",
            status="degraded" if warnings or not confirmed else "complete",
            provenance_value=prov,
            warnings=warnings,
            data=data,
        )


class DataQualityService:
    def read(self) -> dict[str, Any]:
        from core.research_health import data_snapshot

        value = data_snapshot()
        context = value.get("decision_context") or {}
        run_id = f"quality-{payload_hash(value)[:16]}"
        warnings = [
            f"check degraded: {name}" for name, healthy in (value.get("checks") or {}).items()
            if not healthy
        ]
        prov = provenance(
            run_id=run_id,
            decision_at=context.get("decision_at"),
            market_as_of=value.get("actual_market_date"),
            financial_cutoff_at=context.get("financial_cutoff_at"),
            universe_snapshot_id=(value.get("pit_coverage") or {}).get("security_snapshot_id"),
            input_manifest_id=None,
            policy_hash=context.get("policy_hash"),
            code_revision=context.get("code_revision") or code_revision(),
        )
        data = {
            "ready": bool(value.get("ready")),
            "checks": value.get("checks") or {},
            "expected_market_date": value.get("expected_market_date"),
            "actual_market_date": value.get("actual_market_date"),
            "coverage": {
                "market": value.get("usable_qfq_coverage"),
                "valuation": value.get("valuation_coverage"),
                "financial": value.get("financial_coverage"),
            },
        }
        return tool_result(
            summary="Research data is ready." if value.get("ready") else "Research data is degraded.",
            resource_uri=f"shadow://foliant/reports/{run_id}",
            status="complete" if value.get("ready") else "degraded",
            provenance_value=prov,
            warnings=warnings,
            data=data,
        )


class SecurityResearchService:
    capability_read = "foliant.security-research.read"
    capability_preview = "foliant.security-research.preview"

    def __init__(self, coordinator: RunCoordinator | None = None) -> None:
        self.coordinator = coordinator

    def latest_formal(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        from database_pg import db

        record = db.get_latest_record_by_symbol(symbol)
        if not record:
            return tool_result(
                summary=f"No formal research snapshot exists for {symbol}.",
                resource_uri=f"shadow://foliant/securities/{symbol}/research/missing",
                status="missing",
                provenance_value=provenance(run_id="missing", code_revision=code_revision()),
                warnings=["create a preview Run if fresh analysis is required"],
                data=None,
            )
        record_id = str(record.get("id"))
        final_decision = clean_json(record.get("final_decision") or {})
        stock_info = clean_json(record.get("stock_info") or {})
        created_at = str(record.get("created_at") or record.get("analysis_date") or now_iso())
        embedded = final_decision.get("provenance") if isinstance(final_decision, dict) else {}
        embedded = embedded if isinstance(embedded, dict) else {}
        warnings = []
        for key in ("market_as_of", "financial_cutoff_at", "input_manifest_id", "policy_hash"):
            if not embedded.get(key):
                warnings.append(f"legacy formal snapshot does not record {key}")
        prov = provenance(
            run_id=record_id,
            decision_at=embedded.get("decision_at") or created_at,
            market_as_of=embedded.get("market_as_of"),
            financial_cutoff_at=embedded.get("financial_cutoff_at"),
            universe_snapshot_id=embedded.get("universe_snapshot_id"),
            input_manifest_id=embedded.get("input_manifest_id"),
            policy_hash=embedded.get("policy_hash"),
            code_revision=embedded.get("code_revision"),
        )
        rating = final_decision.get("rating") if isinstance(final_decision, dict) else None
        from application.research_artifacts import build_research_artifact

        artifact = build_research_artifact(
            subject=symbol, run_id=record_id, facts=stock_info,
            provenance=prov, data_quality={"level": "legacy" if warnings else "formal"},
            analysis=final_decision, formal=True, created_at=created_at,
        )
        return tool_result(
            summary=f"Latest formal research for {symbol}: rating {rating or 'unavailable'}.",
            resource_uri=f"shadow://foliant/securities/{symbol}/research/{record_id}",
            status="degraded" if warnings else "complete",
            provenance_value=prov,
            warnings=warnings,
            data={"symbol": symbol, "period": record.get("period"), "facts": stock_info,
                  "research_artifact": artifact},
            model_payload={
                "symbol": symbol,
                "period": record.get("period"),
                "rating": rating,
                "facts": stock_info,
                "analysis": final_decision,
            },
            derived_analysis=final_decision,
        )

    def create_preview(self, symbol: str, *, depth: str, actor_id: str,
                       idempotency_key: str, request_id: str = "") -> dict[str, Any]:
        if self.coordinator is None:
            raise RuntimeError("preview Run coordinator is unavailable")
        symbol = normalize_symbol(symbol)
        depth = str(depth or "quick").lower()
        if depth not in {"quick", "deep"}:
            raise ApplicationError("invalid_depth", "depth must be quick or deep")
        request = {"symbol": symbol, "depth": depth, "mode": "preview"}
        return self.coordinator.submit(
            actor_id=actor_id,
            capability=self.capability_preview,
            run_kind="security-research",
            idempotency_key=idempotency_key,
            request_payload=request,
            request_id=request_id,
            resource_uri_factory=(
                lambda run_id: f"shadow://foliant/securities/{symbol}/research/{run_id}"
            ),
            event_type="foliant.research-report.ready",
            runner=lambda run_id: self._analyze_preview(run_id, symbol, depth),
            run_timeout_seconds=600 if depth == "quick" else 1800,
        )

    def compatibility_research(self, symbol: str, *, depth: str = "quick") -> dict[str, Any]:
        """Synchronous local MCP compatibility without making MCP the application boundary."""
        symbol = normalize_symbol(symbol)
        depth = str(depth or "quick").lower()
        if depth not in {"quick", "deep"}:
            raise ApplicationError("invalid_depth", "depth must be quick or deep")
        return self._analyze_preview(f"compat-{payload_hash([symbol, depth])[:20]}", symbol, depth)

    @staticmethod
    def _analyze_preview(run_id: str, symbol: str, depth: str) -> dict[str, Any]:
        from agent_contract import context_quality, context_warnings
        from agent_tool_groups import collect

        groups = ["base", "kline_technical", "fund_flow", "risk"]
        if depth == "deep":
            groups += ["fundamentals", "chan_theory", "chipset", "sentiment"]
        context = collect(groups, symbol)
        quality = context_quality(context)
        warnings = context_warnings(context)
        if quality.get("core_degraded"):
            warnings.append(str((quality.get("guardrails") or {}).get("reason") or "core data degraded"))

        market_as_of = None
        technical = context.get("kline_technical") if isinstance(context, dict) else None
        tail = technical.get("df_tail") if isinstance(technical, dict) else None
        if isinstance(tail, list):
            for row in reversed(tail):
                if isinstance(row, dict):
                    market_as_of = next((str(row.get(key))[:10] for key in
                                         ("trade_date", "date", "Date") if row.get(key)), None)
                    if market_as_of:
                        break
        rendered = copy.deepcopy(context)
        technical = rendered.get("kline_technical") if isinstance(rendered, dict) else None
        if isinstance(technical, dict):
            technical.pop("df_tail", None)
        sentiment = rendered.get("sentiment") if isinstance(rendered, dict) else None
        if isinstance(sentiment, dict) and isinstance(sentiment.get("news"), list):
            sentiment["news"] = sentiment["news"][:5]

        decision_at = now_iso()
        manifest_id = payload_hash({
            "symbol": symbol, "depth": depth, "market_as_of": market_as_of,
            "groups": groups, "code_revision": code_revision(),
        })
        policy = payload_hash({"service": "security-research", "version": "1", "depth": depth})
        prov = provenance(
            run_id=run_id,
            decision_at=decision_at,
            market_as_of=market_as_of,
            financial_cutoff_at=decision_at,
            universe_snapshot_id=payload_hash([symbol]),
            input_manifest_id=manifest_id,
            policy_hash=policy,
            code_revision=code_revision(),
        )
        data = {
            "symbol": symbol,
            "depth": depth,
            "facts": rendered,
            "data_quality": quality,
            "fact_boundary": "facts are returned by deterministic Foliant collectors",
        }
        from application.research_artifacts import build_research_artifact

        artifact = build_research_artifact(
            subject=symbol, run_id=run_id, facts=rendered, provenance=prov,
            data_quality=quality, analysis=None, formal=False,
        )
        data["research_artifact"] = artifact
        return tool_result(
            summary=(f"{depth} preview research for {symbol}; data quality "
                     f"{quality.get('level', 'unknown')}."),
            resource_uri=f"shadow://foliant/securities/{symbol}/research/{run_id}",
            status="degraded" if quality.get("level") == "low" else "complete",
            provenance_value=prov,
            warnings=warnings,
            data=data,
            model_payload={
                "symbol": symbol,
                "depth": depth,
                "data_quality": quality,
                "facts": rendered,
            },
        )


class SelectionRunService:
    capability_read = "foliant.selection.read"
    capability_preview = "foliant.selection.preview"

    def __init__(self, coordinator: RunCoordinator | None = None, *, store: Any = None) -> None:
        self.coordinator = coordinator
        self._store = store

    def _research_store(self):
        if self._store is not None:
            return self._store
        from data.research_store import ResearchStore

        return ResearchStore()

    def latest_formal(self) -> dict[str, Any]:
        latest = self._research_store().latest_formal_selection()
        if not latest:
            return tool_result(
                summary="No formal SelectionRun exists.",
                resource_uri="shadow://foliant/selection-runs/missing",
                status="missing",
                provenance_value=provenance(run_id="missing", code_revision=code_revision()),
                warnings=["selection preview may be created without publishing"],
                data=None,
            )
        run_id = str(latest.get("run_id"))
        metadata = latest.get("metadata") or {}
        context = metadata.get("decision_context") or {}
        warnings = []
        artifacts = latest.get("artifacts") or {}
        if not artifacts.get("formal_top15") or not artifacts.get("formal_top5"):
            warnings.append("formal selection artifacts are incomplete")
        prov = provenance(
            run_id=run_id,
            decision_at=context.get("decision_at"),
            market_as_of=metadata.get("market_as_of"),
            financial_cutoff_at=context.get("financial_cutoff_at"),
            universe_snapshot_id=metadata.get("universe_snapshot_id"),
            input_manifest_id=metadata.get("manifest_id"),
            policy_hash=metadata.get("policy_hash"),
            code_revision=context.get("code_revision"),
        )
        top15 = (artifacts.get("formal_top15") or {}).get("payload") or []
        top5 = (artifacts.get("formal_top5") or {}).get("payload") or []
        overlay = (artifacts.get("display_overlay") or {}).get("payload") or []
        ai_review = (artifacts.get("ai_review") or {}).get("payload") or []
        overlay_by_symbol = {
            str(row.get("symbol") or row.get("code") or ""): row
            for row in overlay if isinstance(row, dict)
        }
        review_by_symbol = {
            str(row.get("symbol") or row.get("code") or ""): row
            for row in ai_review if isinstance(row, dict)
        }

        def decorate(rows):
            return [{
                **overlay_by_symbol.get(str(row.get("symbol") or row.get("code") or ""), {}),
                **review_by_symbol.get(str(row.get("symbol") or row.get("code") or ""), {}),
                **row,
            } for row in rows if isinstance(row, dict)]

        candidates = decorate(top15)
        final_candidates = decorate(top5)
        references = {
            "wencai": (artifacts.get("wencai_strategy_runs") or {}).get("payload") or {},
            "miaoxiang": (artifacts.get("miaoxiang_strategy_runs") or {}).get("payload") or {},
            "miaoxiang_review": (artifacts.get("miaoxiang_review") or {}).get("payload") or {},
        }
        strategy_inputs = {
            "local_strategies": (
                (artifacts.get("local_strategy_nominations") or {}).get("payload") or {}
            ),
            "technical_genome": (
                (artifacts.get("genome_nominations") or {}).get("payload") or {}
            ),
            "fusion_policy": (artifacts.get("fusion_policy") or {}).get("payload") or {},
        }
        payload = {
            "selection_date": latest.get("selection_date"),
            "candidates": candidates,
            "formal_top15": candidates,
            "formal_top5": final_candidates,
            "final_candidates": final_candidates,
            "lane_counts": metadata.get("lane_counts") or {},
            "strategy_inputs": strategy_inputs,
            "references": references,
            "comparison": latest.get("comparison") or {},
            "formal": True,
        }
        return tool_result(
            summary=(f"Formal SelectionRun {run_id} contains {len(candidates)} candidates for "
                     f"{latest.get('selection_date')}."),
            resource_uri=f"shadow://foliant/selection-runs/{run_id}",
            status="degraded" if warnings else "complete",
            provenance_value=prov,
            warnings=warnings,
            data=payload,
            model_payload=payload,
        )

    def create_preview(self, *, selection_date: str | None, decision_mode: str,
                       actor_id: str, idempotency_key: str, request_id: str = "") -> dict[str, Any]:
        if self.coordinator is None:
            raise RuntimeError("preview Run coordinator is unavailable")
        selected = str(selection_date or datetime.now().astimezone().date().isoformat())
        try:
            selected = datetime.fromisoformat(selected).date().isoformat()
        except ValueError as exc:
            raise ApplicationError("invalid_date", "selection_date must be YYYY-MM-DD") from exc
        mode = str(decision_mode or "preopen").lower()
        if mode not in {"preopen", "postclose", "historical_close"}:
            raise ApplicationError("invalid_decision_mode", "unsupported decision_mode")
        request = {"selection_date": selected, "decision_mode": mode, "mode": "preview"}
        return self.coordinator.submit(
            actor_id=actor_id,
            capability=self.capability_preview,
            run_kind="selection",
            idempotency_key=idempotency_key,
            request_payload=request,
            request_id=request_id,
            resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
            event_type="foliant.selection.completed",
            runner=lambda run_id: self._selection_preview(run_id, selected, mode),
            run_timeout_seconds=1800,
        )

    def _selection_preview(self, self_run_id: str, selection_date: str,
                           decision_mode: str) -> dict[str, Any]:
        from analysis.local_stock_selector import LocalStockSelector

        raw = LocalStockSelector(store=self._research_store()).run(
            selection_date,
            decision_mode=decision_mode,
            wencai_reference=None,
            persist=False,
        )
        metadata = raw.get("metadata") or {}
        context = metadata.get("decision_context") or {}
        input_manifest = raw.get("input_manifest") or {}
        warnings = []
        if raw.get("status") != "success":
            warnings.append(str(raw.get("reason") or "selection preview is incomplete"))
        prov = provenance(
            run_id=self_run_id,
            decision_at=context.get("decision_at"),
            market_as_of=metadata.get("market_as_of"),
            financial_cutoff_at=context.get("financial_cutoff_at"),
            universe_snapshot_id=metadata.get("universe_snapshot_id"),
            input_manifest_id=input_manifest.get("manifest_id") or metadata.get("manifest_id"),
            policy_hash=metadata.get("policy_hash"),
            code_revision=context.get("code_revision") or input_manifest.get("code_revision"),
        )
        candidates = raw.get("candidates") or []
        return tool_result(
            summary=(f"Selection preview for {selection_date} produced {len(candidates)} candidates; "
                     "it was not published."),
            resource_uri=f"shadow://foliant/selection-runs/{self_run_id}",
            status="complete" if raw.get("status") == "success" else "degraded",
            provenance_value=prov,
            warnings=warnings,
            data={
                "selection_date": selection_date,
                "formal": False,
                "candidates": candidates,
                "metadata": metadata,
                "input_manifest": input_manifest,
            },
            model_payload={
                "selection_date": selection_date,
                "formal": False,
                "candidates": candidates[:15],
                "input_manifest": input_manifest,
            },
        )


class BacktestRunService:
    capability_preview = "foliant.backtest.preview"

    def __init__(self, coordinator: RunCoordinator | None = None) -> None:
        self.coordinator = coordinator

    def create_preview(self, request: Mapping[str, Any], *, actor_id: str,
                       idempotency_key: str, request_id: str = "") -> dict[str, Any]:
        if self.coordinator is None:
            raise RuntimeError("preview Run coordinator is unavailable")
        raw_codes = request.get("symbols") or request.get("codes") or []
        if not isinstance(raw_codes, list):
            raise ApplicationError("invalid_symbols", "symbols must be an array")
        symbols = list(dict.fromkeys(normalize_symbol(item) for item in raw_codes))
        if not symbols or len(symbols) > 20:
            raise ApplicationError("invalid_symbols", "provide between 1 and 20 symbols")
        today = datetime.now().astimezone().date()
        end = str(request.get("end") or today.isoformat())
        start = str(request.get("start") or (today - timedelta(days=730)).isoformat())
        try:
            start_date = datetime.fromisoformat(start).date()
            end_date = datetime.fromisoformat(end).date()
            if start_date >= end_date or (end_date - start_date).days > 3653:
                raise ValueError
        except ValueError as exc:
            raise ApplicationError(
                "invalid_period", "start must be earlier than end and span no more than 10 years"
            ) from exc
        strategy = str(request.get("strategy") or "enter").strip()
        if not strategy or len(strategy) > 80:
            raise ApplicationError("invalid_strategy", "strategy must contain 1 to 80 characters")
        canonical = {
            "symbols": symbols,
            "strategy": strategy,
            "start": start,
            "end": end,
            "hold_days": _bounded_int(request.get("hold_days"), minimum=1, maximum=250, default=10),
            "stop_pct": _bounded_float(
                request.get("stop_pct", 8.0), minimum=0, maximum=100,
                default=8.0, field="stop_pct",
            ),
            "target_pct": _bounded_float(
                request.get("target_pct", 15.0), minimum=0, maximum=500,
                default=15.0, field="target_pct",
            ),
            "max_positions": _bounded_int(request.get("max_positions"), minimum=1,
                                           maximum=20, default=5),
            "benchmark": normalize_symbol(request.get("benchmark") or "000300"),
            "mode": "preview",
        }
        return self.coordinator.submit(
            actor_id=actor_id,
            capability=self.capability_preview,
            run_kind="backtest",
            idempotency_key=idempotency_key,
            request_payload=canonical,
            request_id=request_id,
            resource_uri_factory=lambda run_id: f"shadow://foliant/backtests/{run_id}",
            event_type="foliant.backtest.completed",
            runner=lambda run_id: self._backtest_preview(run_id, canonical),
            run_timeout_seconds=3600,
        )

    @staticmethod
    def _backtest_preview(run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        from analysis.portfolio_backtest import portfolio_backtest

        result = portfolio_backtest(
            [(symbol, "") for symbol in request["symbols"]],
            request["start"],
            request["end"],
            strategy_id=request["strategy"],
            hold_days=request["hold_days"],
            stop_pct=request["stop_pct"],
            target_pct=request["target_pct"],
            max_positions=request["max_positions"],
            benchmark=request["benchmark"],
            curve_points=240,
            max_workers=4,
        )
        warnings = []
        if result.get("error"):
            warnings.append(f"backtest degraded: {result.get('error')}")
        manifest = payload_hash({**request, "code_revision": code_revision()})
        policy = payload_hash({
            key: request[key] for key in
            ("strategy", "hold_days", "stop_pct", "target_pct", "max_positions", "benchmark")
        })
        prov = provenance(
            run_id=run_id,
            decision_at=now_iso(),
            market_as_of=request["end"],
            financial_cutoff_at=now_iso(),
            universe_snapshot_id=payload_hash(request["symbols"]),
            input_manifest_id=manifest,
            policy_hash=policy,
            code_revision=code_revision(),
        )
        summary = result.get("summary") or {}
        return tool_result(
            summary=(f"Backtest preview completed for {len(request['symbols'])} symbols; "
                     f"{summary.get('trade_count', summary.get('count', 0))} trades."),
            resource_uri=f"shadow://foliant/backtests/{run_id}",
            status="degraded" if warnings else "complete",
            provenance_value=prov,
            warnings=warnings,
            data={"formal": False, "request": request, "result": result},
            model_payload={
                "formal": False,
                "request": request,
                "summary": summary,
                "trades": (result.get("trades") or [])[:50],
                "equity_curve": (result.get("equity_curve") or [])[:120],
            },
        )


class ResearchRunQueryService:
    capability_read = "foliant.run.read"

    def __init__(self, repository: RunRepository) -> None:
        self.repository = repository

    def get(self, run_id: str, *, actor_id: str) -> dict[str, Any]:
        run = self.repository.get(run_id)
        if not run or run.get("actor_id") != actor_id:
            raise ApplicationError("run_not_found", "Run was not found", status_code=404)
        public = _public_run(run)
        public["progress"] = self.repository.progress(run_id)
        return public

    def result(self, run_id: str, *, actor_id: str, offset: int = 0,
               limit: int = 50) -> dict[str, Any]:
        run = self.repository.get(run_id)
        if not run or run.get("actor_id") != actor_id:
            raise ApplicationError("run_not_found", "Run was not found", status_code=404)
        if run.get("status") != "complete":
            raise ApplicationError("run_not_complete", "Run result is not available", status_code=409)
        result = copy.deepcopy(run.get("result_payload") or {})
        offset = max(0, int(offset))
        limit = _bounded_int(limit, minimum=1, maximum=100, default=50)
        continuation = None
        data = result.get("data") if isinstance(result, dict) else None
        container = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
        for key in ("candidates", "trades", "equity_curve", "per_stock", "items"):
            values = container.get(key) if isinstance(container, dict) else None
            if isinstance(values, list):
                total = len(values)
                container[key] = values[offset:offset + limit]
                continuation = {
                    "field": key,
                    "offset": offset,
                    "next_offset": offset + limit if offset + limit < total else None,
                    "limit": limit,
                    "total": total,
                }
                break
        if continuation is not None:
            result["continuation"] = continuation
        if isinstance(result, dict):
            result["model_payload"] = bounded_model_payload({
                "status": result.get("status"),
                "provenance": result.get("provenance") or {},
                "warnings": result.get("warnings") or [],
                "data": result.get("data"),
            })
        return clean_json(result)

    def cancel(self, run_id: str, *, actor_id: str) -> bool:
        return self.repository.cancel(run_id, actor_id)


class TradeEntryService:
    """Stock Web transaction entry; deliberately absent from the Agent research manifest."""

    action = "portfolio.trade-record.create"

    def __init__(self, repository: RunRepository, *, portfolio_db: Any = None) -> None:
        self.repository = repository
        self._portfolio_db = portfolio_db

    def _db(self):
        if self._portfolio_db is not None:
            return self._portfolio_db
        from portfolio_db import portfolio_db

        return portfolio_db

    def preview(self, *, rows: list[dict[str, Any]] | None = None, table: str = "",
                update_position: bool = True, actor_id: str = "preview",
                persist: bool = True,
                source: str = "web:trade-entry") -> dict[str, Any]:
        from portfolio.trade_import_service import (
            prepare_trades,
            preview_position_effects,
            trade_execution_key,
        )

        prepared = prepare_trades(
            rows=rows, table=table, portfolio_db=self._db(), allow_name_refresh=False
        )
        normalized = []
        for item in prepared.get("rows") or []:
            row = dict(item)
            row["source"] = source
            row["external_fingerprint"] = trade_execution_key(row)
            normalized.append(row)
        effects = preview_position_effects(
            normalized, self._db(), update_position=bool(update_position)
        )
        errors = list(prepared.get("errors") or []) + list(effects.get("errors") or [])
        digest_payload = {
            "rows": normalized,
            "update_position": bool(update_position),
            "position_watermark": effects["position_watermark"],
        }
        preview_hash = payload_hash(digest_payload)
        batch_id = "tb_" + payload_hash({
            "actor_id": str(actor_id), "preview_hash": preview_hash,
        })[:24]
        response = {
            "status": "ready" if not errors else "needs_input",
            "batch_id": batch_id,
            "preview_hash": preview_hash,
            "position_watermark": effects["position_watermark"],
            "received": prepared.get("received", 0),
            "prepared": len(normalized),
            "rows": clean_json(normalized),
            "effects": clean_json(effects.get("effects") or []),
            "update_position": bool(update_position),
            "warnings": clean_json(prepared.get("warnings") or []),
            "errors": clean_json(errors),
            "unresolved": clean_json(prepared.get("unresolved") or []),
        }
        db = self._db()
        if persist and not errors and hasattr(db, "stage_trade_import_batch"):
            db.stage_trade_import_batch(
                batch_id=batch_id,
                actor_id=str(actor_id),
                preview_hash=preview_hash,
                position_watermark=effects["position_watermark"],
                update_position=bool(update_position),
                rows=normalized,
            )
        return response

    def confirm(self, *, actor_id: str, idempotency_key: str, preview_hash: str,
                rows: list[dict[str, Any]] | None = None, table: str = "",
                update_position: bool = True, confirmed: bool = False,
                source: str = "web:trade-entry",
                created_by_id: str | None = None) -> dict[str, Any]:
        if not confirmed:
            raise ApplicationError("confirmation_required", "explicit confirmation is required")
        key = str(idempotency_key or "").strip()
        if len(key) < 8 or len(key) > 160:
            raise ApplicationError(
                "idempotency_key_required", "Idempotency-Key must contain 8 to 160 characters"
            )
        preview = self.preview(
            rows=rows, table=table, update_position=update_position,
            actor_id=actor_id, persist=False, source=source,
        )
        if preview.get("errors"):
            raise ApplicationError("invalid_trade", "trade validation failed")
        if not preview_hash or preview_hash != preview.get("preview_hash"):
            raise ApplicationError("preview_mismatch", "trade preview no longer matches")
        request_payload = {
            "batch_id": preview["batch_id"],
            "preview_hash": preview_hash,
            "position_watermark": preview["position_watermark"],
            "rows": preview["rows"],
            "update_position": bool(update_position),
        }
        try:
            claimed, existing = self.repository.claim_write(
                actor_id, self.action, key, request_payload
            )
        except IdempotencyConflict as exc:
            raise ApplicationError("idempotency_conflict", str(exc), status_code=409) from exc
        if not claimed and existing is not None:
            if existing.get("status") == "processing":
                raise ApplicationError(
                    "write_in_progress", "an identical trade write is already in progress",
                    status_code=409,
                )
            return existing

        from portfolio.trade_import_service import import_trade_records

        try:
            confirmed_rows = []
            for item in preview["rows"]:
                row = dict(item)
                row["import_batch_id"] = preview["batch_id"]
                row["created_by_shadow_user_id"] = str(created_by_id or actor_id)
                confirmed_rows.append(row)
            result = import_trade_records(
                rows=confirmed_rows,
                update_position=bool(update_position),
                dry_run=False,
                skip_existing=True,
                portfolio_db=self._db(),
                allow_name_refresh=False,
            )
        except Exception:
            self.repository.release_write_claim(
                actor_id, self.action, key, request_payload
            )
            raise
        response = {
            "status": result.get("status"),
            "batch_id": preview["batch_id"],
            "imported": int(result.get("imported") or 0),
            "failed": int(result.get("failed") or 0),
            "positions_updated": int(result.get("positions_updated") or 0),
            "skipped_existing": int(result.get("skipped_existing") or 0),
            "warnings": clean_json(result.get("warnings") or []),
        }
        if result.get("errors"):
            response["errors"] = ["one or more trade records could not be stored"]
        db = self._db()
        if hasattr(db, "mark_trade_import_batch"):
            db.mark_trade_import_batch(
                preview["batch_id"],
                "confirmed" if response["status"] in {"success", "noop"} else "failed",
            )
        self.repository.save_write_result(actor_id, self.action, key, request_payload, response)
        return response

    def compatibility_import(self, *, rows: list[dict[str, Any]] | None = None,
                             table: str = "", update_position: bool = True,
                             dry_run: bool = False, skip_existing: bool = True) -> dict[str, Any]:
        """Legacy local MCP behavior routed through the application boundary."""
        from portfolio.trade_import_service import import_trade_records

        return import_trade_records(
            rows=rows,
            table=table,
            update_position=update_position,
            dry_run=dry_run,
            skip_existing=skip_existing,
            portfolio_db=self._db(),
        )

    @staticmethod
    def _parse_review_fields(fields: Mapping[str, Any]) -> tuple[list[dict[str, Any]] | None,
                                                                  str, bool]:
        raw_rows = fields.get("trades")
        if raw_rows is None:
            raw_rows = fields.get("rows")
        if raw_rows is None:
            raw_rows = fields.get("tradesJson") or fields.get("rowsJson")
        if isinstance(raw_rows, str):
            try:
                raw_rows = json.loads(raw_rows)
            except json.JSONDecodeError as exc:
                raise ApplicationError("invalid_trades", "trades JSON is invalid") from exc
        rows = raw_rows if isinstance(raw_rows, list) else None
        if rows is not None and any(not isinstance(item, dict) for item in rows):
            raise ApplicationError("invalid_trades", "trades must be an array of objects")
        table = str(fields.get("table") or fields.get("tradeTable") or "").strip()
        if not rows and not table:
            raise ApplicationError("invalid_trades", "trades or table is required")
        raw_update = fields.get("updatePosition", fields.get("update_position", True))
        if isinstance(raw_update, str):
            normalized = raw_update.strip().lower()
            if normalized not in {"true", "false", "1", "0", "yes", "no"}:
                raise ApplicationError("invalid_update_position", "updatePosition is invalid")
            update_position = normalized in {"true", "1", "yes"}
        else:
            update_position = bool(raw_update)
        return rows, table, update_position

    @staticmethod
    def _review_state(status: str) -> str:
        return {
            "staged": "pending", "confirmed": "approved",
            "abandoned": "rejected", "failed": "failed",
        }.get(str(status), "failed")

    def _review_envelope(self, batch: Mapping[str, Any], *, replayed: bool = False,
                         trace_id: str = "") -> dict[str, Any]:
        rows = []
        for item in batch.get("rows") or []:
            payload = item.get("normalized_payload") if isinstance(item, Mapping) else None
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, Mapping):
                rows.append(clean_json(dict(payload)))
        batch_id = str(batch.get("batch_id") or "")
        count = int(batch.get("row_count") or len(rows))
        state = self._review_state(str(batch.get("status") or ""))
        reference = f"shadow://foliant/trade-imports/{batch_id}"
        return {
            "protocol": "shadow.review.v1",
            "review_id": batch_id,
            "reference": reference,
            "revision": 1,
            "domain": "foliant",
            "intent": "foliant.trade.import",
            "summary": f"导入 {count} 条成交记录" + (
                "并更新持仓" if batch.get("update_position") else "，不更新持仓"
            ),
            "fields": {
                "trades": rows,
                "updatePosition": bool(batch.get("update_position")),
                "previewHash": str(batch.get("preview_hash") or ""),
                "positionWatermark": str(batch.get("position_watermark") or ""),
            },
            "risk_level": "L2",
            "state": state,
            "created_at": clean_json(batch.get("created_at") or now_iso()),
            "source_refs": [],
            "trace_id": str(trace_id or ""),
            "receipt": reference if state == "approved" else None,
            "replayed": bool(replayed),
        }

    def create_review(self, *, actor_id: str, fields: Mapping[str, Any],
                      idempotency_key: str, trace_id: str = "") -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if len(key) < 8 or len(key) > 160:
            raise ApplicationError(
                "idempotency_key_required",
                "Idempotency-Key must contain 8 to 160 characters",
            )
        request_payload = {"fields": clean_json(dict(fields))}
        action = "portfolio.trade-review.create"
        try:
            claimed, existing_result = self.repository.claim_write(
                actor_id, action, key, request_payload
            )
        except IdempotencyConflict as exc:
            raise ApplicationError("idempotency_conflict", str(exc), status_code=409) from exc
        if not claimed and existing_result is not None:
            if existing_result.get("status") == "processing":
                raise ApplicationError(
                    "write_in_progress",
                    "an identical trade review is already being created",
                    status_code=409,
                )
            replayed_result = dict(existing_result)
            replayed_result["replayed"] = True
            if trace_id:
                replayed_result["trace_id"] = trace_id
            return replayed_result

        try:
            rows, table, update_position = self._parse_review_fields(fields)
            preview = self.preview(
                rows=rows, table=table, update_position=update_position,
                actor_id=actor_id, persist=False, source="nexus:trade-entry",
            )
            if preview.get("errors"):
                raise ApplicationError("invalid_trade", "trade validation failed")
            db = self._db()
            existing = db.get_trade_import_batch(preview["batch_id"], actor_id)
            replayed = existing is not None
            if existing is None:
                db.stage_trade_import_batch(
                    batch_id=preview["batch_id"], actor_id=actor_id,
                    preview_hash=preview["preview_hash"],
                    position_watermark=preview["position_watermark"],
                    update_position=update_position, rows=preview["rows"],
                )
                existing = db.get_trade_import_batch(preview["batch_id"], actor_id)
            if existing is None:
                raise ApplicationError(
                    "trade_review_unavailable",
                    "trade review could not be stored",
                    status_code=503,
                )
            response = self._review_envelope(
                existing, replayed=replayed, trace_id=trace_id
            )
        except Exception:
            self.repository.release_write_claim(actor_id, action, key, request_payload)
            raise
        self.repository.save_write_result(actor_id, action, key, request_payload, response)
        return response

    def list_reviews(self, *, actor_id: str, limit: int = 100,
                     trace_id: str = "") -> dict[str, Any]:
        batches = self._db().list_trade_import_batches(actor_id, status="staged", limit=limit)
        detailed = [
            self._db().get_trade_import_batch(str(item.get("batch_id") or ""), actor_id)
            for item in batches
        ]
        return {
            "protocol": "shadow.review.v1",
            "items": [
                self._review_envelope(item, trace_id=trace_id)
                for item in detailed if item is not None
            ],
        }

    def get_review(self, review_id: str, *, actor_id: str) -> dict[str, Any]:
        batch = self._db().get_trade_import_batch(review_id, actor_id)
        if batch is None:
            raise ApplicationError("trade_review_not_found", "trade review was not found",
                                   status_code=404)
        return batch

    def commit_review(self, review_id: str, *, actor_id: str, idempotency_key: str,
                      trace_id: str = "") -> dict[str, Any]:
        batch = self.get_review(review_id, actor_id=actor_id)
        if batch.get("status") == "confirmed":
            return self._review_envelope(batch, replayed=True, trace_id=trace_id)
        if batch.get("status") != "staged":
            raise ApplicationError("trade_review_state_invalid",
                                   "trade review is not pending", status_code=409)
        rows = [item.get("normalized_payload") for item in batch.get("rows") or []]
        result = self.confirm(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            preview_hash=str(batch.get("preview_hash") or ""),
            rows=[dict(item) for item in rows if isinstance(item, Mapping)],
            update_position=bool(batch.get("update_position")),
            confirmed=True,
            source="nexus:trade-entry",
        )
        if result.get("status") not in {"success", "noop"}:
            raise ApplicationError("trade_import_failed", "trade import failed", status_code=409)
        refreshed = self.get_review(review_id, actor_id=actor_id)
        return self._review_envelope(refreshed, trace_id=trace_id)

    def reject_review(self, review_id: str, *, actor_id: str,
                      trace_id: str = "") -> dict[str, Any]:
        batch = self.get_review(review_id, actor_id=actor_id)
        if batch.get("status") == "abandoned":
            return self._review_envelope(batch, replayed=True, trace_id=trace_id)
        if batch.get("status") != "staged":
            raise ApplicationError("trade_review_state_invalid",
                                   "trade review is not pending", status_code=409)
        if not self._db().abandon_trade_import_batch(review_id, actor_id):
            raise ApplicationError("trade_review_conflict", "trade review changed", status_code=409)
        refreshed = self.get_review(review_id, actor_id=actor_id)
        return self._review_envelope(refreshed, trace_id=trace_id)


class PortfolioAccessService:
    """Bounded, read-only personal portfolio and trade projections for Nexus."""

    def __init__(self, *, portfolio_db: Any = None) -> None:
        self._portfolio_db = portfolio_db

    def _db(self):
        if self._portfolio_db is not None:
            return self._portfolio_db
        from portfolio_db import portfolio_db

        return portfolio_db

    @staticmethod
    def _local_date(value: Any, timezone: ZoneInfo) -> date | None:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone)
        return value.date()

    def summary(self) -> dict[str, Any]:
        holdings = self._db().get_all_stocks() or []
        active = [row for row in holdings if float(row.get("quantity") or 0) > 0]
        total_cost = sum(
            float(row.get("cost_price") or 0) * float(row.get("quantity") or 0)
            for row in active
        )
        data = {
            "portfolio_ref": "primary",
            "holdings_count": len(active),
            "total_cost": round(total_cost, 2),
            "holdings": clean_json(active[:100]),
        }
        return tool_result(
            summary=f"当前持仓 {len(active)} 只，成本口径合计 {total_cost:.2f} 元",
            resource_uri="shadow://foliant/portfolios/primary",
            status="complete",
            provenance_value=provenance(run_id="portfolio-primary"),
            data=data,
            model_payload=data,
        )

    def trades(self, *, code: str = "", import_date: str = "", trade_date: str = "",
               limit: int = 200, timezone_name: str = "Asia/Shanghai") -> dict[str, Any]:
        if import_date and trade_date:
            raise ApplicationError("invalid_trade_filter",
                                   "import_date and trade_date are mutually exclusive")
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ApplicationError("invalid_timezone", "timezone is invalid") from exc
        target_text = import_date or trade_date
        try:
            target = date.fromisoformat(target_text) if target_text else None
        except ValueError as exc:
            raise ApplicationError("invalid_date", "date must use YYYY-MM-DD") from exc
        bounded_limit = max(1, min(500, int(limit)))
        rows = self._db().get_trades(code or None, 10000 if target else bounded_limit) or []
        field = "created_at" if import_date else "trade_time" if trade_date else ""
        if target is not None:
            rows = [row for row in rows if self._local_date(row.get(field), timezone) == target]
        rows = rows[:bounded_limit]
        cleaned = clean_json(rows)
        data = {
            "portfolio_ref": "primary",
            "filter": {"field": field or "latest", "date": target_text or None,
                       "timezone": timezone_name},
            "count": len(cleaned),
            "records": cleaned,
        }
        return tool_result(
            summary=f"读取到 {len(cleaned)} 条成交记录",
            resource_uri="shadow://foliant/portfolios/primary/trades",
            status="complete",
            provenance_value=provenance(run_id="portfolio-primary-trades"),
            data=data,
            model_payload=data,
        )
