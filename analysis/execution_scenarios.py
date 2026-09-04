"""Six predeclared execution/risk stresses, always against the same frozen order."""
from dataclasses import asdict
from decimal import Decimal
from analysis.decision_evaluation import ExecutionRules, simulate_fill


def execution_scenarios(order, fact, rules, *, cash=None, sellable=0, next_session_fact=None,
                        largest_cluster_weight=None):
    base = asdict(rules)
    doubled = {**base, **{key: str(Decimal(str(base[key])) * 2) for key in
                         ("commission_rate", "minimum_commission", "slippage_bps")}}
    half = {**base, "volume_participation": str(Decimal(base["volume_participation"]) / 2)}
    scenarios = [
        ("double_cost_slippage", fact, ExecutionRules(**doubled), order),
        ("half_liquidity", fact, ExecutionRules(**half), order),
        ("buy_limit_locked", {**fact, "open": fact.get("limit_up")}, rules, {**order, "side": "buy"}),
        ("sell_limit_locked", {**fact, "open": fact.get("limit_down")}, rules, {**order, "side": "sell"}),
        ("one_session_late", next_session_fact, rules, order),
    ]
    output = []
    for name, bar, profile, intent in scenarios:
        fill = simulate_fill(intent, bar, profile, cash=cash, sellable=sellable) if bar else {
            "status": "unavailable", "reason": "next_session_fact_missing"}
        output.append({"scenario": name, "kind": "execution", "result": fill})
    output.append({"scenario": "correlation_concentration", "kind": "risk",
                   "status": "observed" if largest_cluster_weight is not None else "unavailable",
                   "largest_cluster_weight": largest_cluster_weight, "assumption": "cluster moves together; no causal return estimate"})
    return output
