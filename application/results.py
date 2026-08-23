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
