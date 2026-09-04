"""Authorized account read/preview facade shared by Web, MCP and Agent HTTP."""
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd

from analysis.account_action_plan import build_action_plan
from analysis.decision_evaluation import equity_rules
from application.decision_loop import DecisionLoopService
from application.results import clean_json, payload_hash


def preview_account(*, owner_id, available_cash=None, allow_add=False):
    """Load facts server-side; cash override is explicit user-confirmed cash only."""
    if not owner_id:
        raise PermissionError("portfolio_scope_required")
    from portfolio_db import portfolio_db
    import datahub
    service = DecisionLoopService()
    capsule = service.capsule()
    if not capsule:
        return {"status": "missing", "error_code": "formal_capsule_missing", "preview_only": True}
    context = portfolio_db.action_preview_context()
    holdings = context["holdings"]
    holdings = [{**r, "symbol": str(r.get("code") or "")} for r in holdings if float(r.get("quantity") or 0) > 0]
    if len(holdings) > 100:
        raise ValueError("portfolio_preview_size_limit")
    watermark = context["watermark"]
    symbols = sorted({r["symbol"] for r in holdings} | {r["symbol"] for r in capsule["opportunity_set"]["top5"]})
    raw = datahub.quotes(symbols)
    if portfolio_db.action_preview_context()["watermark"] != watermark:
        return {"status": "stale", "error_code": "holdings_changed_during_preview",
                "preview_only": True, "summary": "持仓在取行情期间变化，请重新计算", "alternatives": []}
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    quotes = {}
    for symbol, row in raw.items():
        stamp = row.get("quote_time") or row.get("observed_at")
        try:
            timestamp = pd.to_datetime(str(stamp))
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("Asia/Shanghai")
            rules = equity_rules(symbol)
            quotes[symbol] = {"price": row.get("price"), "observed_at": timestamp.isoformat(),
                              "execution_rules": asdict(rules) if rules else None,
                              "liquidity_budget": max(0, float(row.get("amount_wan") or 0) * 100),
                              "sell_blocked": bool(row.get("limit_down") and row.get("price", 0) <= row["limit_down"]),
                              "buy_blocked": bool(row.get("limit_up") and row.get("price", 0) >= row["limit_up"])}
        except (ValueError, TypeError):
            continue
    for holding in holdings:
        rules = equity_rules(holding["symbol"])
        holding["execution_rules"] = asdict(rules) if rules else None
        # Imported records are not a live broker sellability feed. Leave unknown
        # sellability unknown; never silently treat full holdings as sellable.
        holding.setdefault("sellable", None)
    plan = build_action_plan(capsule, holdings, quotes, holdings_version=watermark,
                             now=now.isoformat(), cash=available_cash, allow_add=allow_add,
                             owner_id=owner_id)
    plan["cash_basis"] = "user_confirmed" if available_cash is not None else "unknown"
    from analysis.portfolio_scenarios import risk_snapshot, stress, explain_actual_formal
    priced = [h for h in holdings if quotes.get(h["symbol"], {}).get("price")]
    snapshot = risk_snapshot(priced, {s: q["price"] for s, q in quotes.items() if q.get("price")}, cash=available_cash)
    snapshot["missing_prices"] = [h["symbol"] for h in holdings if h not in priced]
    snapshot["status"] = "partial" if snapshot["missing_prices"] else "complete"
    plan["risk_snapshot"] = snapshot
    plan["stress_scenarios"] = stress(snapshot) if not snapshot["missing_prices"] else []
    plan["actual_formal_difference"] = explain_actual_formal([h["symbol"] for h in holdings],
        [r["symbol"] for r in capsule["opportunity_set"]["top15"]])
    plan["missing_information"] = (["available_cash"] if available_cash is None else [])
    return plan


def account_books(days=30):
    """One read contract for all private entrypoints; no mixing model and real money."""
    from portfolio.daily_pnl import get_recent
    from analysis.decision_evaluation import money
    days = max(1, min(365, int(days)))
    rows = get_recent(max(days, 60))
    summary = {}
    if rows:
        latest = rows[-1]
        pnls = [money(r["total_daily_pnl"]) for r in rows]
        month = latest["snap_date"][:7]
        summary = {"latest": {k: latest[k] for k in ("snap_date", "total_daily_pnl", "total_daily_pct", "total_mv")},
                   "mtd_pnl": float(sum((money(r["total_daily_pnl"]) for r in rows if r["snap_date"].startswith(month)), money(0))),
                   "period_pnl": float(sum(pnls, money(0))), "period_days": len(rows),
                   "win_rate": round(sum(p > 0 for p in pnls) / len(pnls) * 100, 1),
                   "best_day": float(max(pnls)), "worst_day": float(min(pnls))}
    return {"schema_version": "account-books-v1", "scope": "securities_only",
            "summary": summary, "series": rows[-days:], "snapshot_id": payload_hash(rows),
            "metric_version": "existing-watermarked-account-v1",
            "full_account_reconciliation": "unavailable_without_broker_cash_events",
            "warning": "证券子账户收盘口径；不等于含现金存取的全账户收益，不与模型账本相加"}
