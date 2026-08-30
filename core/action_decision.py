"""Deterministic arbitration for user-facing portfolio actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Optional


ACTION_TEXT = {"add": "加仓", "hold": "不动", "reduce": "减仓", "sell": "卖出"}
_ACTION_ALIASES = {
    "buy": "add", "strong_buy": "add", "add": "add", "加仓": "add", "买入": "add",
    "hold": "hold", "watch": "hold", "avoid": "hold", "不动": "hold",
    "持有": "hold", "观望": "hold",
    "reduce": "reduce", "alert": "reduce", "risk": "reduce",
    "减仓": "reduce", "减持": "reduce", "预警": "reduce",
    "sell": "sell", "卖出": "sell", "清仓": "sell",
}
SOURCE_PRIORITY = {
    "hard_risk": 500,
    "position_truth": 400,
    "portfolio_risk": 300,
    "formal_signal": 200,
    "external_reference": 100,
    "llm": 0,
}
_RISK_ORDER = {"sell": 4, "reduce": 3, "hold": 2, "add": 1}


def normalize_action(value: object) -> str:
    return _ACTION_ALIASES.get(str(value or "").strip().lower(), "hold")


@dataclass(frozen=True)
class ActionEvidence:
    source: str
    action: str
    reason: str = ""
    code: str = ""
    advisory_only: bool = False

    @classmethod
    def from_value(cls, value: object) -> "ActionEvidence":
        if isinstance(value, cls):
            return value
        item = dict(value) if isinstance(value, Mapping) else {}
        source = str(item.get("source") or "external_reference")
        return cls(
            source=source,
            action=normalize_action(item.get("action")),
            reason=str(item.get("reason") or "")[:240],
            code=str(item.get("code") or ""),
            advisory_only=(bool(item.get("advisory_only"))
                           or source in {"llm", "external_reference"}),
        )


def resolve_action(evidence: Iterable[object], *, default: str = "hold") -> dict:
    """Return one action; LLM/reference evidence cannot overrule formal risk facts."""
    items = [ActionEvidence.from_value(value) for value in evidence]
    actionable = [item for item in items if not item.advisory_only]
    if actionable:
        winner = max(
            actionable,
            key=lambda item: (
                SOURCE_PRIORITY.get(item.source, SOURCE_PRIORITY["external_reference"]),
                _RISK_ORDER[normalize_action(item.action)],
            ),
        )
        action = normalize_action(winner.action)
        reason = winner.reason
        source = winner.source
    else:
        action = normalize_action(default)
        reason = ""
        source = "default"

    explanation: Optional[str] = None
    for item in items:
        if item.advisory_only and normalize_action(item.action) == action and item.reason:
            explanation = item.reason
            break
    return {
        "action": action,
        "action_text": ACTION_TEXT[action],
        "reason": reason or explanation or "暂无需要改变仓位的明确信号",
        "explanation": explanation,
        "source": source,
        "suppressed": [
            asdict(item) for item in items
            if item is not winner
        ] if actionable else [asdict(item) for item in items],
    }
