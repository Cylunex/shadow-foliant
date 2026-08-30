"""Focused HTTP routes for runtime data state and versioned research artifacts."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Query


def register_runtime_integrity_routes(
    app: FastAPI,
    *,
    agent_result: Callable[..., Any],
    agent_error: Callable[..., Any],
    browser_ok: Callable[[Any], Any],
) -> None:
    """Register stable compatibility URLs without importing the API composition root."""

    @app.get(
        "/api/machine/v1/agent/market/data-capabilities",
        operation_id="get_agent_data_capabilities",
    )
    def agent_market_data_capabilities():
        try:
            from application.results import now_iso, provenance, tool_result
            from core.decision_context import code_revision
            from data.runtime_capabilities import capability_snapshot

            snapshot = capability_snapshot()
            available = sum(
                1 for endpoints in snapshot.get("providers", {}).values()
                for item in endpoints.values() if item.get("available")
            )
            return agent_result(tool_result(
                summary=f"Runtime market-data capabilities: {available} endpoints available.",
                resource_uri="shadow://foliant/market/data-capabilities",
                status="complete",
                provenance_value=provenance(
                    run_id="runtime-capabilities", decision_at=now_iso(),
                    code_revision=code_revision(),
                ),
                warnings=[],
                data=snapshot,
                model_payload={
                    "dataset_routes": snapshot.get("dataset_routes") or {},
                    "available_endpoint_count": available,
                },
            ), max_bytes=131072)
        except Exception as exc:
            return agent_error(exc)

    @app.get(
        "/api/machine/v1/agent/research-artifacts/latest",
        operation_id="get_agent_research_artifact_latest",
    )
    def agent_research_artifact_latest(subject: str = Query(default="", max_length=160)):
        try:
            from application.results import now_iso, provenance, tool_result
            from application.research_artifacts import latest_research_artifact
            from core.decision_context import code_revision

            value = latest_research_artifact(subject, formal_only=True)
            if value is None:
                return agent_result(tool_result(
                    summary="No formal research artifact is available.",
                    resource_uri="shadow://foliant/research-artifacts/missing",
                    status="missing",
                    provenance_value=provenance(
                        run_id="artifact-missing", decision_at=now_iso(),
                        code_revision=code_revision(),
                    ),
                    warnings=[], data=None,
                ), max_bytes=4096)
            artifact_id = str(value.get("artifact_id") or "unknown")
            return agent_result(tool_result(
                summary=f"Latest formal research artifact for {value.get('subject') or 'subject'}.",
                resource_uri=f"shadow://foliant/research-artifacts/{artifact_id}",
                status="complete",
                provenance_value=value.get("provenance") or provenance(
                    run_id=str(value.get("run_id") or artifact_id),
                    decision_at=value.get("created_at") or now_iso(),
                    code_revision=code_revision(),
                ),
                warnings=[], data={"artifact": value},
                model_payload={
                    "artifact_id": artifact_id,
                    "subject": value.get("subject"),
                    "thesis": value.get("thesis"),
                    "invalidation_conditions": value.get("invalidation_conditions") or [],
                    "data_quality": value.get("data_quality") or {},
                },
            ), max_bytes=131072)
        except Exception as exc:
            return agent_error(exc)

    @app.get("/api/research/data-capabilities")
    def research_data_capabilities():
        from data.runtime_capabilities import capability_snapshot

        return browser_ok(capability_snapshot())

    @app.get("/api/research/artifacts/latest")
    def research_artifact_latest(subject: str = Query(default="", max_length=160)):
        from application.research_artifacts import latest_research_artifact

        return browser_ok(latest_research_artifact(subject, formal_only=True) or {})
