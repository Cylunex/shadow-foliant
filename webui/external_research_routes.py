"""Machine routes for explicit, non-formal external independent research."""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal, Optional

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
Timestamp = Annotated[str, StringConstraints(min_length=20, max_length=64)]
Symbol = Annotated[str, StringConstraints(pattern=r"^(?:\d{6}|(?:sh|sz|bj)?\d{6})$")]


class StrictExternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ExternalEvidenceReq(StrictExternalModel):
    source_url: Annotated[str, StringConstraints(min_length=8, max_length=2000)]
    source_type: Literal["announcement", "policy", "macro", "industry", "news"]
    published_at: Timestamp
    event_at: Optional[Timestamp] = None
    captured_at: Timestamp
    symbols: list[Symbol] = Field(default_factory=list, max_length=100)
    industries: list[ShortText] = Field(default_factory=list, max_length=50)
    direction: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    expiry: Timestamp
    dedupe_key: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    primary_source_confirmed: bool
    controversy_status: Literal["confirmed", "unresolved", "disputed"]


class IndependentOverlayReq(StrictExternalModel):
    symbol: Symbol
    event_adjustment: float = Field(default=0, ge=-15, le=8)
    risk_veto: bool = False
    evidence_ids: list[ShortText] = Field(default_factory=list, max_length=100)


class NewsWatchReq(StrictExternalModel):
    symbol: Symbol
    name: Annotated[str, StringConstraints(max_length=100)] = ""
    industry: Annotated[str, StringConstraints(max_length=100)] = ""
    reason: ShortText
    evidence_ids: list[ShortText] = Field(default_factory=list, max_length=100)


class TuningProposalReq(StrictExternalModel):
    feature: Literal["base_score", "event_adjustment", "risk_veto"]
    mature_sample_count: int = Field(default=0, ge=0)
    covered_weeks: int = Field(default=0, ge=0)
    out_of_sample_delta: float
    expected_impact: ShortText
    acceptance_metric: ShortText
    rollback_condition: ShortText
    time_split_validated: bool = False
    status: Literal["proposed"] = "proposed"


class ExternalIndependentBundleReq(StrictExternalModel):
    channel: Literal["codex-external-independent-v1"]
    selection_run_id: ShortText
    decision_as_of: Timestamp
    market_regime: Literal["bull", "sideways", "bear", "unknown"] = "unknown"
    external_evidence: list[ExternalEvidenceReq] = Field(default_factory=list, max_length=100)
    independent_overlay: list[IndependentOverlayReq] = Field(min_length=15, max_length=15)
    news_watchlist: list[NewsWatchReq] = Field(default_factory=list, max_length=100)
    tuning_proposals: list[TuningProposalReq] = Field(default_factory=list, max_length=50)


def register_external_research_routes(
    app: FastAPI,
    *,
    agent_result: Callable[..., Any],
    agent_error: Callable[..., Any],
) -> None:
    @app.get(
        "/api/machine/v1/agent/external-independent-research",
        operation_id="get_agent_external_independent_research",
    )
    def external_independent_latest():
        try:
            from application.external_research import ExternalIndependentResearchService

            return agent_result(ExternalIndependentResearchService().latest(), max_bytes=262144)
        except Exception as exc:
            return agent_error(exc)

    @app.post(
        "/api/machine/v1/agent/external-independent-research",
        operation_id="save_agent_external_independent_research",
    )
    def external_independent_save(req: ExternalIndependentBundleReq, request: Request):
        try:
            from application.external_research import ExternalIndependentResearchService

            identity = request.state.agent_identity
            value = req.model_dump() if hasattr(req, "model_dump") else req.dict()
            return agent_result(
                ExternalIndependentResearchService().save(
                    value, actor_id=str(identity.agent_id),
                ),
                max_bytes=262144,
            )
        except Exception as exc:
            return agent_error(exc)
