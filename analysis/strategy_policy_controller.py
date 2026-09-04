"""Bounded weekly LLM committee for local-fusion strategy usage.

The model can propose only allow-listed policy changes.  A deterministic validator
checks evidence, concurrency, bounds, change size and the user-defined priority
ordering before a new policy version becomes active.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, Dict, Optional, Tuple

from analysis.local_fusion import FusionPolicy
from data.research_store import ResearchStore


_BOUNDS = {
    "top15_satellite_cap": (0, 5, 1),
    "top15_timing_cap": (0, 2, 1),
    "top5_satellite_cap": (0, 1, 1),
    "top5_timing_cap": (0, 1, 1),
    "genome_min_lane_score": (35.0, 80.0, 5.0),
    "strategy_priority.主力资金": (0.5, 1.0, 0.1),
    "strategy_priority.低价擒牛": (0.5, 1.0, 0.1),
    "strategy_priority.低估值": (0.5, 1.0, 0.1),
    "strategy_priority.小市值": (0.3, 0.8, 0.1),
    "strategy_priority.净利增长": (0.2, 0.6, 0.1),
}


def _extract_json(text: str) -> Optional[dict]:
    cleaned = re.sub(r"```(?:json)?", "", str(text or ""), flags=re.I).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        return None
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_json_key")
            value[key] = item
        return value
    try:
        value = json.loads(match.group(0), object_pairs_hook=unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non_finite_json")))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _get_path(payload: dict, path: str):
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path(payload: dict, path: str, value: object) -> None:
    parts = path.split(".")
    current = payload
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def validate_proposal(proposal: dict, current: dict, evidence: dict,
                      current_hash: str) -> Tuple[bool, str, Optional[dict]]:
    if str(proposal.get("base_policy_hash") or "") != str(current_hash):
        return False, "base_policy_hash不匹配，拒绝旧提案覆盖新政策", None
    if str(proposal.get("evidence_snapshot_id") or "") != str(
        evidence.get("evidence_snapshot_id") or ""
    ):
        return False, "evidence_snapshot_id不匹配", None
    changes = proposal.get("changes")
    if not isinstance(changes, list) or not changes:
        return False, "没有可执行的changes", None
    updated = deepcopy(current)
    seen = set()
    total_steps = 0.
    samples = {
        item.get("strategy_id"): int(item.get("effective_samples") or 0)
        for item in evidence.get("strategies") or []
        if item.get("evidence_kind") == "executable_net"
        and item.get("evidence_policy_hash") == current_hash and item.get("promotion_ready") is True
        and item.get("health_allows_increase", True)
    }
    for change in changes:
        if not isinstance(change, dict):
            return False, "change必须是对象", None
        path = str(change.get("path") or "")
        if path not in _BOUNDS:
            return False, f"路径不在白名单:{path}", None
        if path in seen:
            return False, f"重复路径:{path}", None
        seen.add(path)
        old = _get_path(current, path)
        previous = change.get("from")
        if (old is None or isinstance(previous, bool)
                or not isinstance(previous, (int, float)) or previous != old):
            return False, f"from与当前政策不一致:{path}", None
        new = change.get("to")
        if isinstance(new, bool) or not isinstance(new, (int, float)) or not math.isfinite(new):
            return False, f"to必须是数字:{path}", None
        if isinstance(old, int) and not isinstance(new, int):
            return False, f"配额必须是整数:{path}", None
        lower, upper, max_delta = _BOUNDS[path]
        if not lower <= float(new) <= upper:
            return False, f"超出边界:{path}", None
        if abs(float(new) - float(old)) > max_delta + 1e-9:
            return False, f"单周变化过大:{path}", None
        total_steps += abs(float(new) - float(old)) / max_delta
        if total_steps > 3 + 1e-9:
            return False, "单次政策调整累计不得超过3个标准步长", None
        # Increasing production exposure needs actual matured evidence.  Reductions
        # remain fast so a weak strategy can be contained immediately.
        risk_increase = new < old if path == "genome_min_lane_score" else new > old
        if risk_increase:
            relevant = "technical_timing_genome" if "timing" in path or "genome" in path else None
            if relevant and samples.get(relevant, 0) < 20:
                return False, f"增加基因组暴露至少需要20个成熟样本:{path}", None
            if path in {"top15_satellite_cap", "top5_satellite_cap"} or path.startswith("strategy_priority."):
                # Concurrent strategies share decision dates, so summing their
                # counts would manufacture independence. Specific weight changes
                # require that strategy's evidence; quota changes use a maximum.
                from analysis.local_reference_strategies import STRATEGY_CONFIG
                if path.startswith("strategy_priority."):
                    sid = STRATEGY_CONFIG[path.split(".", 1)[1]]["strategy_id"]
                    local_samples = samples.get(sid, 0)
                else:
                    local_samples = max((count for sid, count in samples.items() if sid.startswith("local_")
                                         and sid != "local_pit_v4"), default=0)
                if local_samples < 30:
                    return False, "增加本地卫星名额至少需要30个成熟样本", None
        _set_path(updated, path, int(new) if isinstance(old, int) else float(new))

    priorities = updated.get("strategy_priority") or {}
    high_floor = min(float(priorities.get(name, 0)) for name in ("主力资金", "低价擒牛", "低估值"))
    if not high_floor > float(priorities.get("小市值", 0)) > float(priorities.get("净利增长", 0)):
        return False, "必须保持主力资金/低价擒牛/低估值 > 小市值 > 净利增长", None
    if int(updated.get("top15_core_floor") or 0) < 8:
        return False, "PIT核心下限被锁定为至少8只", None
    return True, "validated", updated


def run_weekly_committee(store: Optional[ResearchStore] = None,
                         *, call_llm: bool = True) -> dict:
    store = store or ResearchStore()
    store.update_selection_candidate_outcomes()
    evidence = store.selection_strategy_evidence(horizon_days=5, lookback_days=180)
    active = store.load_active_strategy_policy()
    current = deepcopy((active or {}).get("payload") or FusionPolicy().as_dict())
    current_hash = str((active or {}).get("policy_hash") or hashlib.sha256(
        json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest())
    from application.model_evidence import model_strategy_evidence
    from application.results import payload_hash
    content_hash = payload_hash(current)
    executable = model_strategy_evidence(store, content_hash)
    from application.strategy_health import refresh_health
    health = refresh_health(store, executable)
    health_by_id = {r["object_id"]: r for r in health}
    # Old registry rows may predate canonical hashing. Retain their identity for
    # CAS, but bind model evidence to exactly the same normalized policy content.
    for item in executable["strategies"]:
        item["health_allows_increase"] = health_by_id.get(str(item["strategy_id"]), {}).get("allow_risk_increase", False)
        item["evidence_content_hash"] = content_hash
        item["evidence_policy_hash"] = current_hash
    evidence["strategies"] = list(evidence.get("strategies") or []) + executable["strategies"]
    evidence["executable_evidence"] = executable
    evidence["evidence_snapshot_id"] = payload_hash(evidence)
    if not call_llm:
        return {"status": "evidence_only", "policy_hash": current_hash,
                "policy": current, "evidence": evidence}

    system = (
        "你是A股本地选股策略委员会。只能根据给定统计提出有界策略使用政策，"
        "不得修改选股代码、因子公式、PIT核心下限，不得让问财或妙想进入正式候选。"
        "只输出严格JSON。样本不足时输出changes为空。"
    )
    prompt = {
        "base_policy_hash": current_hash,
        "policy": current,
        "evidence": evidence,
        "allowed_paths": _BOUNDS,
        "requirements": [
            "每周每项最多变化一个步长",
            "主力资金/低价擒牛/低估值权重必须高于小市值，小市值高于净利增长",
            "收益为负或回撤恶化时优先降级；晋升需足够成熟样本",
        ],
        "output_schema": {
            "base_policy_hash": current_hash,
            "evidence_snapshot_id": evidence["evidence_snapshot_id"],
            "changes": [{"path": "...", "from": 0, "to": 0}],
            "reason": "...",
            "proposal_confidence": 0.0,
        },
    }
    try:
        from llm_router import get_router
        from application.research_budget import committee_call
        call_result = committee_call(store, prompt=prompt, system=system, call=get_router().call)
        if call_result["status"] != "complete":
            return {**call_result, "evidence": evidence}
        text, provider = call_result["text"], call_result["provider"]
    except Exception as exc:
        return {"status": "llm_unavailable", "error": type(exc).__name__,
                "evidence": evidence}
    proposal = _extract_json(text)
    if proposal is None:
        return {"status": "invalid_llm_json", "provider": provider,
                "evidence_snapshot_id": evidence["evidence_snapshot_id"]}
    proposal.setdefault("proposal_id", hashlib.sha256(
        f"{datetime.now().date()}:{current_hash}:{evidence['evidence_snapshot_id']}:"
        f"{json.dumps(proposal, sort_keys=True, default=str)}"
        .encode("utf-8")
    ).hexdigest())
    # Timing is a controller decision, never an LLM-controlled string. Activation
    # starts before the next confirmed trading session, not on a natural weekend.
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT MIN(trade_date) FROM research_trade_calendar WHERE trade_date>?",
                    (datetime.now().date().isoformat(),))
        row = cur.fetchone()
        effective_day = str(row[0]) if row and row[0] else None
    finally:
        conn.close()
    proposal["effective_from"] = f"{effective_day}T09:00:00+08:00" if effective_day else None
    from application.strategy_health import record_policy_arms
    record_policy_arms(store, current=current, current_hash=current_hash, evidence=evidence, llm_proposal=proposal)
    if not proposal.get("changes"):
        store.save_strategy_policy_proposal(
            proposal, validation_status="no_change", validation_reason="LLM建议维持现状"
        )
        return {"status": "no_change", "provider": provider, "proposal": proposal,
                "evidence": evidence}
    valid, reason, updated = validate_proposal(proposal, current, evidence, current_hash)
    if valid and not effective_day:
        valid, reason, updated = False, "缺少下一交易日共识，暂不发布政策", None
    if valid:
        updated["version"] = "local-fusion-" + str(proposal["proposal_id"])[:16]
    store.save_strategy_policy_proposal(
        proposal, validation_status="applied" if valid else "rejected",
        validation_reason=reason, applied_policy=updated if valid else None,
    )
    return {"status": "applied" if valid else "rejected", "provider": provider,
            "reason": reason, "proposal": proposal,
            "policy": updated if valid else current, "evidence": evidence}
