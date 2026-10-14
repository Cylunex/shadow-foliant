"""One source for scheduled pick identity and display decisions.

Only non-sensitive digests and source state enter the delivery ledger. Prices,
scores, capture times and run IDs never define a changed displayed result.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PICK_POLICY_VERSION = "morning-picks-change-only-v1"
SOURCES = (("正式", "formal_selection", "formal_top5"),
           ("独立量化", "independent_selection", "top5"),
           ("外部研究", "external_independent_research", "top5"))


def _symbol(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    value = str(row.get("symbol") or row.get("code") or "").strip()
    return value.zfill(6) if value.isdigit() and len(value) <= 6 else ""


def selection_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a verified ordered result; incomplete inputs cannot revoke picks."""
    day = str((snapshot.get("trading_day") or {}).get("date") or "")
    formal = snapshot.get("formal_selection") or {}
    independent = snapshot.get("independent_selection") or {}
    if (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or formal.get("status") != "complete"
            or formal.get("selection_date") != day
            or not formal.get("run_id")
            or independent.get("status") != "complete"
            or independent.get("selection_session_date") != day):
        return {"verified": False, "fingerprint": None, "sources": ""}
    external = snapshot.get("external_independent_research") or {}
    source_rows: list[tuple[str, list[str]]] = []
    for label, section_name, key in SOURCES:
        section = snapshot.get(section_name) or {}
        if label == "外部研究":
            if section.get("status") != "complete":
                continue
            if (not section.get("overlay_id")
                    or section.get("selection_run_id") != formal.get("run_id")):
                return {"verified": False, "fingerprint": None, "sources": ""}
        rows = section.get(key)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
            return {"verified": False, "fingerprint": None, "sources": ""}
        symbols = [_symbol(row) for row in rows]
        if not all(symbols) or len(set(symbols)) != len(symbols):
            return {"verified": False, "fingerprint": None, "sources": ""}
        source_rows.append((label, symbols))
    # Both primary sources are required. A disappearing optional source is
    # handled against the accepted baseline in decide_selection below.
    if len(source_rows) < 2:
        return {"verified": False, "fingerprint": None, "sources": ""}
    canonical = json.dumps(source_rows, ensure_ascii=False, separators=(",", ":"))
    origin = json.dumps({"formal_run": formal.get("run_id"),
                         "independent_input": independent.get("input_snapshot_id"),
                         "external_overlay": external.get("overlay_id")
                         if external.get("status") == "complete" else None},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"verified": True,
            "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "origin_fingerprint": hashlib.sha256(origin.encode("utf-8")).hexdigest(),
            "selection_run_id": str(formal.get("run_id")),
            "research_id": str(external.get("overlay_id"))
            if external.get("status") == "complete" else None,
            "sources": "+".join(label for label, _ in source_rows),
            "ordered_sources": source_rows}


def decide_selection(slot: str, current: dict[str, Any], state: dict[str, Any]) -> bool:
    """Morning once; later only a verified change from accepted same-day picks."""
    if not current.get("verified") or not current.get("fingerprint"):
        return False
    if slot.endswith("T10:15+08:00"):
        return True
    if state.get("uncertain_pick_attempt"):
        return False
    baseline = state.get("accepted_fingerprint")
    baseline_origin = state.get("accepted_origin_fingerprint")
    baseline_sources = state.get("accepted_sources") or ""
    if baseline:
        if any(source not in current["sources"].split("+")
               for source in baseline_sources.split("+") if source):
            return False
        return bool(current["fingerprint"] != baseline and baseline_origin
                    and current.get("origin_fingerprint") != baseline_origin)
    # A failed morning attempt is not a delivered baseline. It still proves
    # which result was available then, so only a genuinely changed result can
    # be announced later. An absent morning record gives no such evidence.
    morning = state.get("morning_fingerprint")
    morning_origin = state.get("morning_origin_fingerprint")
    return bool(morning and current["fingerprint"] != morning and morning_origin
                and current.get("origin_fingerprint") != morning_origin)
