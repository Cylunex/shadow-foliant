"""Deterministic portfolio stress views; descriptive, never causal claims."""
from copy import deepcopy
from decimal import Decimal
from analysis.decision_evaluation import money


SCENARIOS = {
    "broad_selloff": {"default": Decimal("-0.06")},
    "growth_selloff": {"default": Decimal("-0.02"), "科技": Decimal("-0.10"), "成长": Decimal("-0.10")},
    "liquidity_gap": {"default": Decimal("-0.04")},
    "limit_down_lock": {"default": Decimal("-0.03")},
    "industry_shock": {"largest_industry": Decimal("-0.12"), "default": Decimal("-0.01")},
    "policy_reversal": {"default": Decimal("-0.05")},
}


def risk_snapshot(holdings, prices, *, cash=None):
    rows, total = [], Decimal(0)
    for row in holdings:
        symbol = str(row["symbol"])
        value = money(prices[symbol]) * int(row["quantity"])
        total += value
        rows.append({"symbol": symbol, "value": value, "industry": row.get("industry") or "unknown",
                     "themes": row.get("themes") or ["unknown"]})
    nav = total + (money(cash) if cash is not None else 0)
    industries = {}
    for row in rows:
        industries[row["industry"]] = industries.get(row["industry"], Decimal(0)) + row["value"]
    return {"nav": str(nav), "securities_value": str(total), "cash_known": cash is not None,
            "position_weights": {r["symbol"]: float(r["value"] / nav) for r in rows} if nav else {},
            "industry_weights": {k: float(v / nav) for k, v in industries.items()} if nav else {},
            "largest_industry": max(industries, key=industries.get) if industries else None,
            "rows": [{**r, "value": str(r["value"])} for r in rows]}


def stress(snapshot):
    nav = money(snapshot["nav"])
    if nav <= 0:
        return []
    output = []
    for name, shocks in SCENARIOS.items():
        pnl = Decimal(0)
        for row in snapshot["rows"]:
            shock = shocks.get(row["industry"], shocks.get("default", 0))
            if name == "industry_shock" and row["industry"] == snapshot["largest_industry"]:
                shock = shocks["largest_industry"]
            pnl += money(row["value"]) * shock
        output.append({"scenario": name, "pnl": str(money(pnl)), "return_pct": float(pnl / nav * 100),
                       "assumption": "fixed first-order price shock; no liquidity or causal inference"})
    return output


def non_trade_band(*, price, rules, expected_edge_pct, uncertainty_pct, minimum_lots=1):
    if uncertainty_pct < 0 or expected_edge_pct < 0:
        raise ValueError("invalid_edge_or_uncertainty")
    gross = money(price) * rules.buy_step * minimum_lots
    from analysis.decision_evaluation import execution_cost
    round_trip_pct = float((execution_cost(gross, "buy", rules) + execution_cost(gross, "sell", rules)) / gross * 100)
    required = round_trip_pct + uncertainty_pct
    return {"action": "trade" if expected_edge_pct > required else "hold", "expected_edge_pct": expected_edge_pct,
            "required_edge_pct": required, "cost_pct": round_trip_pct, "uncertainty_pct": uncertainty_pct,
            "reason": "edge_exceeds_cost_and_uncertainty" if expected_edge_pct > required else "inside_no_trade_band"}


def explain_actual_formal(actual, formal):
    a, f = set(actual), set(formal)
    return {"overlap": sorted(a & f), "actual_only": sorted(a - f), "formal_only": sorted(f - a),
            "causal_claim": False, "explanation": "描述账户与正式候选的差异；不能据此认定收益差由名单造成。"}
