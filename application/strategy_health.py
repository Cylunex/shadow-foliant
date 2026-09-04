"""Evidence health with hysteresis. Missing evidence does not disable incumbents."""
from datetime import datetime, timedelta
from data.reliability_store import ReliabilityStore, utcnow


def transition(previous, evidence, *, now, hard_risk=False):
    previous = previous or {"state": "active", "bad_streak": 0, "good_streak": 0}
    if hard_risk or previous.get("state") == "risk_halted":
        return {**previous, "state": "risk_halted", "allow_risk_increase": False,
                "reason": "deterministic_hard_risk", "as_of": now}
    if evidence.get("status") != "sufficient":
        return {**previous, "evidence_state": "insufficient_evidence", "allow_risk_increase": False, "as_of": now}
    lower = evidence.get("conservative_lower_bound_pct")
    good = evidence.get("promotion_ready") is True
    bad = lower is not None and lower < 0
    result = {**previous, "bad_streak": previous.get("bad_streak", 0) + 1 if bad else 0,
              "good_streak": previous.get("good_streak", 0) + 1 if good else 0,
              "evidence_state": "sufficient", "as_of": now, "allow_risk_increase": False}
    if result["bad_streak"] >= 2:
        result.update(state="cooling", cooldown_until=(datetime.fromisoformat(now) + timedelta(days=14)).isoformat())
    elif result["good_streak"] >= 3 and now >= previous.get("cooldown_until", "") and previous.get("state") != "risk_halted":
        result.update(state="active", allow_risk_increase=True)
    return result


def refresh_health(store, evidence, *, now=None):
    repo = ReliabilityStore(store)
    now = now or utcnow()
    output = []
    for row in evidence.get("strategies", []):
        symbol = str(row["strategy_id"])
        old = repo.get("strategy_health", symbol)
        # Same evidence snapshot/day is not another independent review streak.
        key = row.get("evidence_snapshot_id") or evidence.get("evidence_snapshot_id")
        if old and old.get("snapshot_id") == key:
            output.append(old)
            continue
        value = transition(old, row, now=now)
        output.append(repo.put("strategy_health", symbol, {**value, "snapshot_id": key},
                               expected_revision=(old or {}).get("revision", 0)))
    return output


def record_policy_arms(store, *, current, current_hash, evidence, llm_proposal):
    """No-change, deterministic containment and bounded LLM use one controller."""
    from copy import deepcopy
    from analysis.strategy_policy_controller import validate_proposal
    from analysis.local_reference_strategies import STRATEGY_CONFIG
    from application.results import payload_hash
    repo = ReliabilityStore(store)
    health = {r["object_id"]: r for r in repo.list("strategy_health")}
    changes = []
    for name, info in STRATEGY_CONFIG.items():
        if health.get(info["strategy_id"], {}).get("state") == "cooling":
            old = current["strategy_priority"][name]
            changes.append({"path": "strategy_priority." + name, "from": old, "to": round(old - .05, 2)})
            break  # At most one predeclared containment step, no tuning search.
    rule = {"base_policy_hash": current_hash, "evidence_snapshot_id": evidence["evidence_snapshot_id"], "changes": changes}
    rv, rr, rp = validate_proposal(rule, current, evidence, current_hash)
    lv, lr, lp = validate_proposal(llm_proposal, current, evidence, current_hash)
    value = {"initial_policy_hash": current_hash, "evidence_snapshot_id": evidence["evidence_snapshot_id"],
             "arms": {"no_adjustment": deepcopy(current), "rules_only": rp if rv else deepcopy(current),
                      "rules_and_llm": lp if lv else deepcopy(current)},
             "decisions": {"rules_only": {"accepted": rv, "reason": rr}, "rules_and_llm": {"accepted": lv, "reason": lr}},
             "created_at": utcnow(), "causal_claim": False}
    return repo.once("policy_study", payload_hash(value), value)
