"""Pure cash/security/NAV ledger with explicit corporate-action completeness."""
from copy import deepcopy
from decimal import Decimal
from analysis.decision_evaluation import money, price_metrics
from application.results import payload_hash


def empty_ledger(initial_cash="100000.00"):
    return {"schema_version": "model-ledger-v1", "initial_cash": str(money(initial_cash)),
            "cash": str(money(initial_cash)), "positions": {}, "events": [], "marks": [],
            "fees": "0.00", "scope": "full_account", "revision": 0}


def apply_fill(ledger, *, event_id, symbol, side, fill):
    value = deepcopy(ledger)
    if event_id in value["events"]:
        return value
    if fill.get("status") not in {"filled", "partially_filled"}:
        return value
    qty = fill["quantity"]
    if type(qty) is not int or qty <= 0 or side not in {"buy", "sell"}:
        raise ValueError("invalid_fill")
    cash = money(value["cash"]) + money(fill["cash_delta"])
    current = value["positions"].get(symbol, {"quantity": 0, "last_buy_date": None})
    remaining = current["quantity"] + qty * (1 if side == "buy" else -1)
    if cash < 0 or remaining < 0:
        raise ValueError("ledger_cash_or_security_overdraw")
    value["positions"][symbol] = {"quantity": remaining, "last_buy_date":
        fill["executed_at"][:10] if side == "buy" else current["last_buy_date"]}
    value["cash"] = str(cash)
    value["fees"] = str(money(value["fees"]) + money(fill["fees"]))
    value["events"].append(event_id)
    value["revision"] += 1
    return value


def apply_corporate_action(ledger, event):
    """Raw-share ledger only: adjusted-price returns must never also add dividends.

    Entitlement quantity is the record-date position, not today's position. Cash is
    credited on payment date by the caller; split fractions require explicit cash
    in lieu. Revisions create new compensating events, never overwrite old ones.
    """
    value = deepcopy(ledger)
    identity = event.get("event_id")
    if not identity:
        raise ValueError("corporate_action_id_required")
    if identity in value["events"]:
        return value
    if event.get("price_basis") != "raw" or not event.get("confirmed"):
        raise ValueError("unverified_corporate_action")
    if event["kind"] == "cash_dividend":
        entitlement = event["entitled_quantity"]
        if type(entitlement) is not int or entitlement < 0:
            raise ValueError("invalid_dividend_entitlement")
        credit = money(Decimal(str(event["net_cash_per_share"])) * entitlement)
        if credit < 0:
            raise ValueError("negative_dividend_requires_compensating_event")
        value["cash"] = str(money(value["cash"]) + credit)
    elif event["kind"] == "split":
        position = value["positions"].get(event["symbol"])
        if not position:
            raise ValueError("split_position_missing")
        quantity = Decimal(position["quantity"]) * Decimal(str(event["ratio"]))
        if quantity != int(quantity) or quantity <= 0:
            raise ValueError("fractional_split_requires_explicit_settlement")
        position["quantity"] = int(quantity)
    else:
        raise ValueError("corporate_action_not_supported")
    value["events"].append(identity)
    value["revision"] += 1
    return value


def mark_ledger(ledger, *, trade_date, prices, corporate_actions_complete=False):
    value = deepcopy(ledger)
    if value["marks"] and trade_date <= value["marks"][-1]["trade_date"]:
        raise ValueError("model_mark_must_advance")
    market_value = Decimal(0)
    missing = []
    for symbol, position in value["positions"].items():
        if not position["quantity"]:
            continue
        if symbol not in prices or money(prices[symbol]) <= 0:
            missing.append(symbol)
            continue
        market_value += money(prices[symbol]) * position["quantity"]
    nav = money(value["cash"]) + market_value if not missing else None
    mark = {"trade_date": trade_date, "net_asset_value": str(money(nav)) if nav is not None else None,
            "policy_hash": value.get("policy_hash"),
            "cash": value["cash"], "fees_paid": value["fees"], "missing_prices": missing,
            "corporate_actions_complete": bool(corporate_actions_complete),
            "status": "verified" if not missing and corporate_actions_complete else "indicative"}
    mark["net_return_pct"] = float((nav / money(value["initial_cash"]) - 1) * 100) if nav is not None else None
    value["marks"].append(mark)
    observed = [float(v["net_asset_value"]) for v in value["marks"] if v["net_asset_value"] is not None]
    # Missing marks can hide intervening peaks; do not label a partial series MDD.
    mark["nav_max_drawdown_pct"] = price_metrics(float(value["initial_cash"]), observed)["close_max_drawdown_pct"] if observed and len(observed) == len(value["marks"]) else None
    value["revision"] += 1
    value["state_hash"] = payload_hash({k: v for k, v in value.items() if k != "state_hash"})
    return value
