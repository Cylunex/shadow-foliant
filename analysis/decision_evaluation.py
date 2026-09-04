"""Versioned, pure execution/measurement primitives; never place real orders."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import math
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
METRIC_VERSION = "signal-price-v2"


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non_finite_money")
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def unit_price(value) -> Decimal:
    """Keep quoted precision (e.g. ETF 1.408); only currency totals use cents."""
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("invalid_unit_price")
    return result


def price_metrics(entry, closes, lows=()):
    """Percentage points. MAE includes zero; MDD is close-to-close peak drawdown."""
    entry = float(entry)
    values = [float(v) for v in closes]
    adverse = [float(v) for v in lows]
    if entry <= 0 or not values or not all(math.isfinite(v) and v > 0 for v in [entry] + values + adverse):
        raise ValueError("invalid_price_series")
    peak, drawdown = entry, 0.
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, (value / peak - 1) * 100)
    return {"metric_version": METRIC_VERSION,
            "price_return_pct": (values[-1] / entry - 1) * 100,
            "mae_pct": min([0.] + [(v / entry - 1) * 100 for v in adverse]),
            "close_max_drawdown_pct": drawdown,
            "return_kind": "signal_price_observation", "executable_round_trip": False}


@dataclass(frozen=True)
class ExecutionRules:
    """Explicit instrument snapshot; missing rules must not be inferred from names.

    Commission is a modelling assumption, not the user's brokerage fee schedule.
    Price limits must be supplied for the instrument/day; no fixed 10% shortcut.
    """
    version: str = "a-share-execution-v1"
    min_buy: int = 100
    buy_step: int = 100
    sell_step: int = 100
    min_sell: int = 100
    t_plus: int = 1
    commission_rate: str = "0.0003"
    minimum_commission: str = "5"
    sell_tax_rate: str = "0.0005"
    transfer_rate: str = "0.00001"
    slippage_bps: str = "10"
    volume_participation: str = "0.01"

    def __post_init__(self):
        for value in (self.min_buy, self.buy_step, self.sell_step, self.min_sell):
            if type(value) is not int or value <= 0:
                raise ValueError("invalid_lot_rule")
        for value in (self.commission_rate, self.minimum_commission, self.sell_tax_rate,
                      self.transfer_rate, self.slippage_bps, self.volume_participation):
            parsed = Decimal(str(value))
            if not parsed.is_finite() or parsed < 0:
                raise ValueError("invalid_cost_rule")
        if not 0 < Decimal(self.volume_participation) <= 1 or self.t_plus not in (0, 1):
            raise ValueError("invalid_execution_rule")


def execution_cost(gross, side, rules: ExecutionRules):
    value = money(gross)
    if side not in {"buy", "sell"} or value < 0:
        raise ValueError("invalid_order")
    if not value:
        return Decimal("0.00")
    commission = max(Decimal(rules.minimum_commission), value * Decimal(rules.commission_rate))
    return money(commission + value * Decimal(rules.transfer_rate) +
                 (value * Decimal(rules.sell_tax_rate) if side == "sell" else 0))


def equity_rules(symbol):
    """Versioned equity profiles; ETFs/bonds/unknown instruments need metadata."""
    symbol = str(symbol)
    if len(symbol) != 6 or not symbol.isdigit():
        return None
    if symbol.startswith("688"):
        return ExecutionRules(min_buy=200, buy_step=1, sell_step=1, min_sell=200)
    if symbol.startswith(("60", "00", "30")):
        return ExecutionRules()
    if symbol.startswith(("920", "4", "8")):
        return ExecutionRules(min_buy=100, buy_step=1, sell_step=1)
    return None


def simulate_fill(order: dict, bar: dict, rules: ExecutionRules, *, cash=None, sellable=0):
    """One declared next-open attempt. Raw data and published daily limits required.

    Daily volume is an ex-post capacity ceiling, not proof of opening liquidity.
    Fees are charged once per simulated attempt. No close-price fallback.
    """
    base = {"execution_model": "next-open-raw-v1", "rules": asdict(rules),
            "quantity": 0, "fees": "0.00", "status": "unfilled"}
    def reject(reason):
        return {**base, "reason": reason}
    side = order.get("side")
    qty = order.get("quantity")
    if side not in {"buy", "sell"} or type(qty) is not int or qty <= 0:
        return reject("invalid_order")
    execution_at = str(bar.get("trade_date") or "") + "T09:30:00+08:00"
    try:
        published = datetime.fromisoformat(order["published_at"])
        earliest = datetime.fromisoformat(order["earliest_execution_at"])
        execution = datetime.fromisoformat(execution_at)
        if published.tzinfo is None or earliest.tzinfo is None:
            return reject("timezone_missing")
        if execution <= published or execution < earliest:
            return reject("before_publication")
    except (ValueError, KeyError, TypeError):
        return reject("execution_time_missing")
    if bar.get("adjustment") != "raw" or bar.get("corporate_action_unresolved"):
        return reject("raw_or_corporate_action_missing")
    if bar.get("suspended"):
        return reject("suspended")
    try:
        opening = Decimal(str(bar["open"]))
        volume = Decimal(str(bar["volume"]))
        upper, lower = Decimal(str(bar["limit_up"])), Decimal(str(bar["limit_down"]))
        if not all(v.is_finite() and v > 0 for v in (opening, volume, upper, lower)):
            return reject("invalid_market_data")
    except (KeyError, ValueError, ArithmeticError):
        return reject("execution_data_missing")
    if side == "buy" and opening >= upper or side == "sell" and opening <= lower:
        return reject("limit_locked")
    price = money(opening * (1 + Decimal(rules.slippage_bps) / 10000 * (1 if side == "buy" else -1)))
    if not lower <= price <= upper:
        return reject("slippage_outside_limit")
    capacity = int(volume * Decimal(rules.volume_participation))
    quantity = min(qty, capacity, sellable if side == "sell" else qty)
    step = rules.buy_step if side == "buy" else rules.sell_step
    # Odd-lot disposal is allowed only for the entire sellable residue.
    if side != "sell" or quantity != sellable:
        quantity = quantity // step * step
    if side == "buy":
        if cash is None:
            return reject("cash_unknown")
        available = money(cash)
        quantity = min(quantity, int(max(Decimal(0), available) / price) // step * step)
        while quantity >= rules.min_buy and price * quantity + execution_cost(price * quantity, side, rules) > available:
            quantity -= step
        if quantity < rules.min_buy:
            return reject("cash_or_lot_constraint")
    if quantity <= 0:
        return reject("liquidity_or_sellable_constraint")
    if side == "sell" and quantity < rules.min_sell and quantity != sellable:
        return reject("sell_lot_constraint")
    gross = money(price * quantity)
    fees = execution_cost(gross, side, rules)
    return {**base, "status": "filled" if quantity == qty else "partially_filled",
            "quantity": quantity, "price": str(price), "gross": str(gross),
            "fees": str(fees), "executed_at": execution_at,
            "cash_delta": str(-gross - fees if side == "buy" else gross - fees),
            "assumption": "daily_volume_capacity_not_opening_fill_guarantee"}


def reconcile_book(*, opening_cash, closing_cash, opening_market_value,
                   closing_market_value, events, scope="full_account"):
    """Account boundary is explicit. Exact cents, immutable event IDs required."""
    if scope not in {"full_account", "securities_only"}:
        raise ValueError("account_scope_required")
    seen, cash_effect, external, security_flow = set(), Decimal(0), Decimal(0), Decimal(0)
    investment_cash = Decimal(0)
    for event in events:
        identity = event.get("event_id")
        if not identity or identity in seen:
            raise ValueError("missing_or_duplicate_event_id")
        seen.add(identity)
        amount = money(event["cash_delta"])
        cash_effect += amount
        if event["kind"] in {"deposit", "withdrawal"}:
            external += amount
        elif event["kind"] in {"buy", "sell"}:
            security_flow -= amount
        elif event["kind"] not in {"dividend", "fee", "interest", "tax"}:
            raise ValueError("unclassified_cash_event")
        else:
            investment_cash += amount
    residual = money(closing_cash) - money(opening_cash) - cash_effect
    mv_delta = money(closing_market_value) - money(opening_market_value)
    pnl = (mv_delta - security_flow + investment_cash if scope == "securities_only" else
           mv_delta + money(closing_cash) - money(opening_cash) - external)
    return {"metric_version": "account-reconciliation-v1", "scope": scope,
            "pnl": str(money(pnl)), "cash_residual": str(money(residual)),
            "external_flow": str(money(external)), "securities_flow": str(money(security_flow)),
            "status": "reconciled" if residual == 0 else "mismatch"}
