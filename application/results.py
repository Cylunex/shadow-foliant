"""Stable application result and provenance helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

RUN_STATUSES = {"queued", "running", "complete", "failed", "cancelled"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_json(value: Any) -> Any:
    """Convert common scientific values into strict JSON without exposing object reprs."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(item) for item in value]
    if hasattr(value, "item"):
        try:
            return clean_json(value.item())
        except Exception:  # noqa: BLE001 - third-party scalar conversion is best effort
            return None
    if isinstance(value, datetime):
        return value.astimezone().isoformat(timespec="seconds")
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        clean_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bounded_model_payload(value: Any, *, max_chars: int = 9000) -> Any:
    """Return a deterministic, structured projection that fits model context budgets."""

    cleaned = clean_json(value)
    if len(canonical_json(cleaned)) <= max_chars:
        return cleaned

    def shrink(item: Any, *, list_limit: int, string_limit: int, depth: int = 0) -> Any:
        if depth >= 8:
            return "[depth-truncated]"
        if isinstance(item, str):
            return item if len(item) <= string_limit else item[:string_limit] + "…"
        if isinstance(item, list):
            values = [
                shrink(child, list_limit=list_limit, string_limit=string_limit, depth=depth + 1)
                for child in item[:list_limit]
            ]
            if len(item) > list_limit:
                values.append({"truncated_items": len(item) - list_limit})
            return values
        if isinstance(item, dict):
            return {
                key: shrink(child, list_limit=list_limit, string_limit=string_limit,
                            depth=depth + 1)
                for key, child in item.items()
            }
        return item

    for list_limit, string_limit in ((20, 800), (10, 400), (5, 200), (3, 120)):
        projected = shrink(cleaned, list_limit=list_limit, string_limit=string_limit)
        if isinstance(projected, dict):
            projected["model_payload_truncated"] = True
        if len(canonical_json(projected)) <= max_chars:
            return projected
    return {
        "model_payload_truncated": True,
        "payload_hash": payload_hash(cleaned),
        "summary": "Structured result exceeds the model projection budget; use pagination.",
    }


def provenance(
    *,
    run_id: str,
    decision_at: str | None = None,
    market_as_of: str | None = None,
    financial_cutoff_at: str | None = None,
    universe_snapshot_id: str | None = None,
    input_manifest_id: str | None = None,
    policy_hash: str | None = None,
    code_revision: str | None = None,
) -> dict[str, str | None]:
    return {
        "decision_at": decision_at or now_iso(),
        "market_as_of": market_as_of,
        "financial_cutoff_at": financial_cutoff_at,
        "universe_snapshot_id": universe_snapshot_id,
        "input_manifest_id": input_manifest_id,
        "policy_hash": policy_hash,
        "code_revision": code_revision,
        "run_id": run_id,
    }


def tool_result(
    *,
    summary: str,
    resource_uri: str,
    status: str,
    provenance_value: Mapping[str, Any],
    warnings: list[str] | None = None,
    data: Any = None,
    model_payload: Any = None,
    continuation: Mapping[str, Any] | None = None,
    derived_analysis: Any = None,
) -> dict[str, Any]:
    result = {
        "summary": str(summary)[:2000],
        "resource_uri": str(resource_uri),
        "status": str(status),
        "provenance": clean_json(dict(provenance_value)),
        "warnings": [str(item)[:500] for item in (warnings or [])],
        "data": clean_json(data),
    }
    if model_payload is not None:
        result["model_payload"] = bounded_model_payload(model_payload)
    if continuation:
        result["continuation"] = clean_json(dict(continuation))
    if derived_analysis is not None:
        result["derived_analysis"] = clean_json(derived_analysis)
    return result


def stable_failure(exc: BaseException) -> str:
    """Map implementation failures without persisting exception text."""
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_input"
    if isinstance(exc, TimeoutError):
        return "execution_timeout"
    return "execution_failed"
