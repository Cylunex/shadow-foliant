"""Constraint-first account previews; separate from immutable formal selection."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from application.results import payload_hash
from analysis.decision_evaluation import ExecutionRules, execution_cost, money


@dataclass(frozen=True)
class AccountLimits:
    max_position: float = .15
    max_industry: float = .35
    max_theme: float = .45
    max_exposure: float = .90
    max_turnover: float = .10
    quote_ttl_seconds: int = 120

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key != "quote_ttl_seconds" and (isinstance(value, bool) or not 0 < value <= 1):
                raise ValueError("invalid_account_limit")
        if not 1 <= self.quote_ttl_seconds <= 300:
            raise ValueError("invalid_quote_ttl")


def model_portfolio(capsule, *, max_weight=.2):
    """Equal-budget benchmark with explicit cash, not a rewrite of TOP5 order."""
    if not 0 < max_weight <= 1:
        raise ValueError("invalid_model_weight")
    rows = capsule["opportunity_set"]["top5"]
    weight = min(max_weight, 1 / len(rows)) if rows else 0
    value = {"schema_version": "model-portfolio-v1", "capsule_id": capsule["capsule_id"],
             "weights": {str(r["symbol"]): weight for r in rows},
             "cash_weight": 1 - len(rows) * weight, "model": "equal_capped",
             "published_at": capsule["published_at"]}
    value["model_id"] = "mp_" + payload_hash(value)
    return value


def build_action_plan(capsule, holdings, quotes, *, holdings_version, now,
                      cash=None, allow_add=False, limits=None, owner_id=None, _include_replace=True):
    """Generate mutually exclusive previews with shared constraints and no writes.

    Holdings/quotes/instrument rules must be authorized server-loaded facts. Unknown
    cash or sellability is a blocker, never auto-filled with invented values.
    """
    if not owner_id or not holdings_version:
        raise ValueError("authorized_holdings_context_required")
    limits = limits or AccountLimits()
    clock = datetime.fromisoformat(now)
    if clock.tzinfo is None:
        raise ValueError("timezone_required")
    failures = []
    held = {str(h["symbol"]): deepcopy(h) for h in holdings}

    def price_for(symbol):
        quote = quotes.get(symbol) or {}
        try:
            age = (clock - datetime.fromisoformat(quote["observed_at"])).total_seconds()
            price = money(quote["price"])
            if not 0 <= age <= limits.quote_ttl_seconds or price <= 0:
                return None
            return price
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return None

    position_values, industry_values, theme_values = {}, {}, {}
    for symbol, holding in held.items():
        price = price_for(symbol)
        if price is None:
            failures.append(f"stale_holding_quote:{symbol}")
            continue
        value = price * int(holding["quantity"])
        position_values[symbol] = value
        industry = holding.get("industry") or "unknown"
        industry_values[industry] = industry_values.get(industry, Decimal(0)) + value
        for theme in holding.get("themes") or ["unknown"]:
            theme_values[theme] = theme_values.get(theme, Decimal(0)) + value
    mv = sum(position_values.values(), Decimal(0))
    balance = money(cash) if cash is not None else None
    nav = mv + balance if balance is not None else None
    if balance is None:
        failures.append("cash_unknown")
    elif balance < 0 or nav <= 0:
        failures.append("invalid_account_balance")
    alternatives = [{"kind": "hold", "actions": [], "feasible": True,
                     "reason": "不动也是有效选择"}]
    reductions = []
    if nav and nav > 0:
        turnover_budget = nav * Decimal(str(limits.max_turnover))
        industry_over = {key: max(Decimal(0), value - nav * Decimal(str(limits.max_industry))) for key, value in industry_values.items()}
        theme_over = {key: max(Decimal(0), value - nav * Decimal(str(limits.max_theme))) for key, value in theme_values.items()}
        for symbol, value in sorted(position_values.items(), key=lambda item: -item[1]):
            holding = held[symbol]
            industry = holding.get("industry") or "unknown"
            themes = holding.get("themes") or ["unknown"]
            over = max(value - nav * Decimal(str(limits.max_position)),
                       industry_over.get(industry, 0), *(theme_over.get(theme, 0) for theme in themes))
            if over <= 0 or holding.get("sellable") is None:
                continue
            price = price_for(symbol)
            rules_data = holding.get("execution_rules")
            if not rules_data:
                continue
            rules = ExecutionRules(**rules_data)
            liquid = quotes.get(symbol, {}).get("liquidity_budget")
            if liquid is None or money(liquid) <= 0 or quotes.get(symbol, {}).get("sell_blocked"):
                continue
            qty = min(int(holding["sellable"]), int(min(over, turnover_budget, money(liquid)) / price))
            qty = qty // rules.sell_step * rules.sell_step
            if qty < rules.min_sell and qty != holding["sellable"]:
                continue
            if qty > 0:
                reductions.append({"symbol": symbol, "side": "sell", "quantity": qty,
                                   "reason": "降低单票、行业或主题超限暴露"})
                turnover_budget -= price * qty
                industry_over[industry] = max(Decimal(0), industry_over.get(industry, 0) - price * qty)
                for theme in themes:
                    theme_over[theme] = max(Decimal(0), theme_over.get(theme, 0) - price * qty)
    alternatives.append({"kind": "reduce", "actions": reductions,
                         "feasible": bool(reductions) and not failures,
                         "reason": "按已知可卖数量减少超限持仓；未核实可卖的持仓不生成卖单"})
    additions, rejected = [], []
    if allow_add and not failures:
        spend = min(balance, max(Decimal(0), nav * Decimal(str(limits.max_exposure)) - mv),
                    nav * Decimal(str(limits.max_turnover)))
        for row in capsule["opportunity_set"]["top5"]:
            symbol = str(row["symbol"])
            price = price_for(symbol)
            industry = row.get("industry") or "unknown"
            quote = quotes.get(symbol) or {}
            reason = None
            if price is None:
                reason = "quote_stale_or_missing"
            elif not quote.get("execution_rules"):
                reason = "instrument_rules_missing"
            elif quote.get("suspended") or quote.get("buy_blocked"):
                reason = "not_tradable"
            elif not quote.get("liquidity_budget"):
                reason = "liquidity_unknown"
            if reason:
                rejected.append({"symbol": symbol, "reason": reason})
                continue
            rules = ExecutionRules(**quote["execution_rules"])
            budget = min(spend, nav * Decimal(str(limits.max_position)) - position_values.get(symbol, 0),
                         nav * Decimal(str(limits.max_industry)) - industry_values.get(industry, 0),
                         money(quote["liquidity_budget"]))
            for theme in row.get("themes") or ["unknown"]:
                budget = min(budget, nav * Decimal(str(limits.max_theme)) - theme_values.get(theme, 0))
            qty = max(0, int(budget / price)) // rules.buy_step * rules.buy_step
            while qty >= rules.min_buy and price * qty + execution_cost(price * qty, "buy", rules) > budget:
                qty -= rules.buy_step
            if qty < rules.min_buy:
                rejected.append({"symbol": symbol, "reason": "cash_exposure_lot_or_turnover_constraint"})
                continue
            from analysis.portfolio_scenarios import non_trade_band
            if quote.get("expected_edge_pct") is not None and quote.get("uncertainty_pct") is not None:
                band = non_trade_band(price=price, rules=rules, expected_edge_pct=quote["expected_edge_pct"],
                                      uncertainty_pct=quote["uncertainty_pct"])
                if band["action"] == "hold":
                    rejected.append({"symbol": symbol, "reason": "inside_no_trade_band", "cost_comparison": band})
                    continue
            cost = price * qty + execution_cost(price * qty, "buy", rules)
            additions.append({"symbol": symbol, "side": "buy", "quantity": qty,
                              "estimated_cost": str(money(cost)), "reason": "正式候选通过账户约束",
                              "estimated_fees": str(execution_cost(price * qty, "buy", rules)),
                              "net_edge": None, "edge_basis": "unknown_unless_separately_verified"})
            spend -= cost
            position_values[symbol] = position_values.get(symbol, 0) + price * qty
            industry_values[industry] = industry_values.get(industry, 0) + price * qty
            for theme in row.get("themes") or ["unknown"]:
                theme_values[theme] = theme_values.get(theme, 0) + price * qty
    alternatives.append({"kind": "add", "actions": additions, "feasible": bool(additions),
                         "reason": "通过约束，可考虑分批增持" if additions else "没有合格加仓标的，暂不操作"})
    replacement = {"kind": "replace", "actions": [], "feasible": False,
                   "reason": "暂不满足替换条件；不预支未成交的卖出资金"}
    if _include_replace and reductions and allow_add and not failures:
        projected = deepcopy(holdings)
        projected_cash = balance
        sold_value = Decimal(0)
        for order in reductions:
            position = next(h for h in projected if str(h["symbol"]) == order["symbol"])
            gross = price_for(order["symbol"]) * order["quantity"]
            rules = ExecutionRules(**position["execution_rules"])
            projected_cash += gross - execution_cost(gross, "sell", rules)
            sold_value += gross
            position["quantity"] -= order["quantity"]
            position["sellable"] -= order["quantity"]
        remaining_turnover = limits.max_turnover - float(sold_value / nav)
        if remaining_turnover > 0:
            projected_plan = build_action_plan(
                capsule, projected, quotes, holdings_version=holdings_version, now=now,
                cash=projected_cash, allow_add=True, owner_id=owner_id, _include_replace=False,
                limits=AccountLimits(**{**asdict(limits), "max_turnover": remaining_turnover}))
            buys = next(a["actions"] for a in projected_plan["alternatives"] if a["kind"] == "add")
            if buys:
                replacement.update(actions=reductions + buys, feasible=True, conditional=True,
                    preconditions=["confirm_sell_fills", "refresh_cash_holdings_and_quotes", "revalidate_plan"],
                    reason="条件式替换预览：先减超限持仓，成交后重新核对资金和行情再考虑增持")
    alternatives.append(replacement)
    result = {"schema_version": "account-action-plan-v1", "scope": "portfolio",
              "owner_id": owner_id, "capsule_id": capsule["capsule_id"],
              "holdings_version": holdings_version, "created_at": now,
              "expires_at": (clock + timedelta(seconds=limits.quote_ttl_seconds)).isoformat(),
              "quote_fingerprint": payload_hash(quotes), "limits": asdict(limits),
              "preview_only": True, "alternatives": alternatives, "blockers": failures,
              "rejected_candidates": rejected,
              "summary": "可考虑分批加仓" if additions else "没有合格加仓标的，暂不操作" if allow_add else "未请求加仓，先观察账户约束"}
    result["plan_id"] = "ap_" + payload_hash(result)
    return result


def plan_valid(plan, *, holdings_version, quotes, now, owner_id):
    if plan["owner_id"] != owner_id:
        raise PermissionError("portfolio_scope_required")
    return (plan["holdings_version"] == holdings_version and
            plan["quote_fingerprint"] == payload_hash(quotes) and
            datetime.fromisoformat(plan["created_at"]) <= datetime.fromisoformat(now) <=
            datetime.fromisoformat(plan["expires_at"]))
