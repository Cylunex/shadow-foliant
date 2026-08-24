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
    try:
        value = json.loads(match.group(0))
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
    samples = {
        item.get("strategy_id"): int(item.get("sample_size") or 0)
        for item in evidence.get("strategies") or []
    }
    for change in changes:
        if not isinstance(change, dict):
            return False, "change必须是对象", None
        path = str(change.get("path") or "")
        if path not in _BOUNDS:
            return False, f"路径不在白名单:{path}", None
        old = _get_path(updated, path)
        if old is None or change.get("from") != old:
            return False, f"from与当前政策不一致:{path}", None
        new = change.get("to")
        if not isinstance(new, (int, float)):
            return False, f"to必须是数字:{path}", None
        lower, upper, max_delta = _BOUNDS[path]
        if not lower <= float(new) <= upper:
            return False, f"超出边界:{path}", None
        if abs(float(new) - float(old)) > max_delta + 1e-9:
            return False, f"单周变化过大:{path}", None
        # Increasing production exposure needs actual matured evidence.  Reductions
        # remain fast so a weak strategy can be contained immediately.
        if float(new) > float(old):
            relevant = "technical_timing_genome" if "timing" in path or "genome" in path else None
            if relevant and samples.get(relevant, 0) < 20:
                return False, f"增加基因组暴露至少需要20个成熟样本:{path}", None
            if path == "top15_satellite_cap":
                local_samples = sum(count for sid, count in samples.items() if sid.startswith("local_")
                                    and sid != "local_pit_v4")
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
        text, provider = get_router().call(
            [{"role": "system", "content": system},
             {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            temperature=0.1, max_tokens=1800, timeout=90,
            call_type="strategy_policy_committee",
        )
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
    proposal.setdefault("effective_from", (datetime.now().date() + timedelta(days=1)).isoformat())
    if not proposal.get("changes"):
        store.save_strategy_policy_proposal(
            proposal, validation_status="no_change", validation_reason="LLM建议维持现状"
        )
        return {"status": "no_change", "provider": provider, "proposal": proposal,
                "evidence": evidence}
    valid, reason, updated = validate_proposal(proposal, current, evidence, current_hash)
    store.save_strategy_policy_proposal(
        proposal, validation_status="applied" if valid else "rejected",
        validation_reason=reason, applied_policy=updated if valid else None,
    )
    return {"status": "applied" if valid else "rejected", "provider": provider,
            "reason": reason, "proposal": proposal,
            "policy": updated if valid else current, "evidence": evidence}
