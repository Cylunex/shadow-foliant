"""Account observations from available daily facts, independent of posterior jobs.

This is a read-only diagnosis, not a selector, execution plan or alert producer.
Recorded intraday triggers retain their original date and protected final action.
"""
from __future__ import annotations

from datetime import datetime
import math
from zoneinfo import ZoneInfo

from application.results import clean_json, payload_hash
from application.market_prices import current_day_price

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_REVIEW_SCHEMA = "daily-account-review-v1"
DAILY_REVIEW_POLICY = {
    "requires_current_day_evidence": True,
    "posterior_completeness_required": False,
    "minimum_mature_samples": 0,
    "missing_inputs_block_only_dependent_adjustments": True,
    "execution_gates_unchanged": True,
    "auto_execution": False,
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else
                parsed.astimezone(SHANGHAI))
    except (TypeError, ValueError):
        return None


def _today(value, now):
    instant = _instant(value)
    return bool(instant and instant.date() == now.date() and instant <= now)


def holdings_fingerprint(holdings):
    """Bind a holdings-only reference plan to the exact current account facts."""
    rows = []
    for row in holdings:
        symbol = str(row.get("symbol") or row.get("code") or "").zfill(6)
        quantity = _number(row.get("quantity", row.get("shares")))
        if not symbol.isdigit() or len(symbol) != 6 or quantity is None or quantity <= 0:
            continue
        rows.append({"symbol": symbol, "quantity": quantity,
                     "cost_price": _number(row.get("cost_price", row.get("cost")))})
    return payload_hash(sorted(rows, key=lambda row: row["symbol"]))


def build_daily_review(*, now, trading_day, holdings, quotes, formal, previous,
                       holdings_available=True):
    now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    base = {"schema_version": DAILY_REVIEW_SCHEMA, "review_date": now.date().isoformat(),
            "as_of": now.isoformat(timespec="seconds"), "policy": dict(DAILY_REVIEW_POLICY),
            "preview_only": True, "auto_execution": False,
            "formal_membership_unchanged": True, "holdings": [], "operations": [],
            "candidates": []}
    if trading_day.get("confirmed") and not trading_day.get("is_trading_day"):
        return base | {"status": "not_applicable", "holding_observed_count": 0,
                       "candidate_observed_count": 0}
    quote_map = {str(row.get("symbol") or ""): row for row in quotes}
    formal_current = (formal.get("status") in {"complete", "success"}
                      and formal.get("selection_date") == now.date().isoformat())
    base["holdings_available"] = holdings_available
    base["formal_selection_available"] = formal_current
    previous_current = bool(str(previous.get("trade_date") or "")[:10] == now.date().isoformat()
                            and _today(previous.get("generated_at"), now))
    old_rows = {str(row.get("symbol") or ""): row
                for row in previous.get("holdings") or [] if isinstance(row, dict)} if previous_current else {}
    states = previous.get("trigger_state") or {} if previous_current else {}

    def quote_fact(symbol):
        quote = quote_map.get(symbol) or {}
        return quote, current_day_price(quote, now)

    rows, operations = [], []
    for holding in holdings[:100]:
        symbol = str(holding.get("symbol") or holding.get("code") or "")
        quote, price = quote_fact(symbol)
        cost, quantity = _number(holding.get("cost_price")), _number(holding.get("quantity"))
        pnl = (price - cost) if price is not None and cost is not None and cost > 0 else None
        row = {"symbol": symbol, "name": holding.get("name"), "quantity": quantity,
               "status": "observed" if price is not None else "missing_quote",
               "current_price": price, "quote_as_of": quote.get("as_of"),
               "change_pct": _number(quote.get("change_pct")) if price is not None else None,
               "holding_pnl_pct": round(pnl / cost * 100, 2) if pnl is not None else None,
               "unrealized_pnl_cny": round(pnl * quantity, 2)
               if pnl is not None and quantity is not None and quantity > 0 else None,
               "realized_pnl_cny": None, "recorded_triggers": []}
        old = old_rows.get(symbol) or {}
        requested = str(old.get("requested_action") or old.get("action") or "")
        final = str(old.get("action") or "")
        old_action_current = _today(old.get("quote_as_of"), now)
        for kind in ("stop", "target"):
            state = states.get(f"{symbol}:{kind}") or {}
            if _today(state.get("last_alerted_at"), now):
                row["recorded_triggers"].append({"kind": kind,
                    "observed_at": state["last_alerted_at"], "still_active": bool(state.get("active"))})
        if old_action_current:
            row["previous_requested_action"] = requested
            row["previous_final_action"] = final
            row["previous_action_as_of"] = old.get("quote_as_of")
            if old.get("requested_source") == "hard_risk" and requested in {"sell", "reduce"}:
                if not any(x["kind"] == "stop" for x in row["recorded_triggers"]):
                    row["recorded_triggers"].append({"kind": "stop",
                        "observed_at": old["quote_as_of"], "still_active": None})
        kinds = {x["kind"] for x in row["recorded_triggers"]}
        if kinds or (old_action_current and final in {"sell", "reduce"}):
            kind = "stop" if "stop" in kinds else "target" if "target" in kinds else "risk_action"
            # A trigger recovered by the last quote is still a daily observation.
            # It never reinstates a guarded sell or creates a current execution.
            protected = bool(old_action_current and requested in {"sell", "reduce"} and final == "hold")
            operations.append({"symbol": symbol, "name": holding.get("name"),
                "quantity": quantity, "action": final if old_action_current else "hold",
                "requested_action": requested if old_action_current else "hold",
                "historical_trigger": kind, "protected": protected,
                "observation_only": True, "executable": False,
                "observed_at": (old.get("quote_as_of") if old_action_current else
                                row["recorded_triggers"][0]["observed_at"]),
                "decision_source": "hard_risk" if kind == "stop" else "daily_recorded_observation",
                "reason": "日内已记录风险，须用新行情和有效计划复核；不代表已成交"})
        rows.append(row)

    candidates = []
    if formal_current:
        for candidate in (formal.get("formal_top15") or [])[:15]:
            symbol = str(candidate.get("symbol") or "")
            quote, price = quote_fact(symbol)
            plan = candidate.get("trade_plan") or {}
            rr = _number(plan.get("risk_reward_ratio")) if plan.get("available") is True else None
            candidates.append({"symbol": symbol, "name": candidate.get("name"),
                "formal_rank": candidate.get("rank"), "current_price": price,
                "quote_as_of": quote.get("as_of"), "status": "observed" if price is not None else "missing_quote",
                "change_pct": _number(quote.get("change_pct")) if price is not None else None,
                "reference_plan_risk_reward_ratio": rr, "reference_plan_as_of": plan.get("plan_as_of"),
                "reference_plan_action": plan.get("action"), "reference_plan_blockers": plan.get("blockers") or [],
                "execution_ready": False})
    observed = sum(row["status"] == "observed" for row in rows)
    candidate_observed = sum(row["status"] == "observed" for row in candidates)
    complete = (bool(rows or candidates) and holdings_available and trading_day.get("confirmed") and formal_current
                and observed == len(rows) and candidate_observed == len(candidates))
    protected_counts = {action: sum(row.get("previous_requested_action") == action
                                   and row.get("previous_final_action") == "hold" for row in rows)
                        for action in ("sell", "reduce")}
    return clean_json(base | {"status": "complete" if complete else "partial" if observed or candidate_observed or operations else "missing",
        "holding_count": len(rows), "holding_observed_count": observed,
        "candidate_count": len(candidates), "candidate_observed_count": candidate_observed,
        "recorded_protected_counts": protected_counts,
        "recorded_operation_count": len(operations), "holdings": rows,
        "operations": operations, "candidates": candidates})
