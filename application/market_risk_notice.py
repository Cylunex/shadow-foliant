"""Shared, quote-time-verified market context and informational alert policy.

This policy describes market information. It never grants account execution.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
from typing import Any, Callable
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
VERSION = "market-risk-information-v1"
INDEX_CODES = {"上证指数": "sh000001", "深证成指": "sz399001",
               "创业板指": "sz399006", "科创50": "sh000688",
               "沪深300": "sh000300"}
MAIN = {"上证指数", "深证成指", "沪深300"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _limit(name: str, default: float, low: float, high: float) -> float:
    value = _number(os.getenv(name, default))
    return min(high, max(low, value)) if value is not None else default


def _stamp(value: Any) -> datetime | None:
    try:
        raw = str(value or "")
        parsed = (datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
                  if raw.isdigit() and len(raw) >= 14 else datetime.fromisoformat(raw))
        return parsed.astimezone(SHANGHAI) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def current_indices(now: datetime, loader: Callable | None = None,
                    *, post_close: bool = False) -> dict[str, Any]:
    """Read existing provider's full index quotes; reject receipt-time-only data."""
    if loader is None:
        from data.sources.tencent import quotes
        loader = quotes
    now = now.astimezone(SHANGHAI)
    try:
        raw = loader(list(INDEX_CODES.values())) or {}
    except Exception:
        raw = {}
    rows = []
    for name, code in INDEX_CODES.items():
        value = raw.get(code) or raw.get(code[2:]) or {}
        quote_at = _stamp(value.get("quote_time")) if isinstance(value, dict) else None
        pct = _number(value.get("change_pct")) if isinstance(value, dict) else None
        price = _number(value.get("price")) if isinstance(value, dict) else None
        if (quote_at is None or quote_at.date() != now.date()
                or quote_at > now + timedelta(minutes=1)
                or now - quote_at > timedelta(minutes=8 if not post_close else 360)
                or (post_close and (quote_at.hour, quote_at.minute) < (15, 0))
                or pct is None or not -25 <= pct <= 25 or price is None or price <= 0):
            continue
        rows.append({"name": name, "change_pct": round(pct, 3),
                     "as_of": quote_at.isoformat(timespec="seconds"),
                     "source": "tencent_full_index_quote", "time_source": "provider"})
    return {"version": VERSION, "status": "current" if rows else "unavailable",
            "rows": rows, "observed_at": now.isoformat(timespec="seconds")}


def risk_level(market: dict[str, Any]) -> int:
    rows = market.get("rows") or []
    main = _limit("FOLIANT_MARKET_MAIN_DROP_PCT", 1.5, 1.0, 3.0)
    growth = _limit("FOLIANT_MARKET_GROWTH_DROP_PCT", 2.5, 1.5, 4.0)
    triggers = [row for row in rows if isinstance(row, dict) and
                (pct := _number(row.get("change_pct"))) is not None and
                pct <= -(main if row.get("name") in MAIN else growth)]
    if not triggers:
        return 0
    return 2 if len(triggers) >= 3 else 1


def market_lines(market: dict[str, Any]) -> list[str]:
    rows = [row for row in market.get("rows") or [] if isinstance(row, dict)]
    if not rows:
        return ["市场：当前宽基行情待核验"]
    stamps = [stamp for row in rows if (stamp := _stamp(row.get("as_of")))]
    time = min(stamps).strftime("%H:%M") if stamps else "待核验"
    detail = "、".join(f"{row['name']}{row['change_pct']:+.2f}%" for row in rows[:5])
    if risk_level(market):
        triggered = [row for row in rows if row["change_pct"] <= -(
            _limit("FOLIANT_MARKET_MAIN_DROP_PCT", 1.5, 1.0, 3.0)
            if row["name"] in MAIN else
            _limit("FOLIANT_MARKET_GROWTH_DROP_PCT", 2.5, 1.5, 4.0))]
        lead = ("市场明显走弱，先核对风险、谨慎追涨" if len(triggered) >= 2
                else f"{triggered[0]['name']}明显下跌，先核对风险、谨慎追涨")
    else:
        lead = (f"指数基调：{rows[0]['name']}当日涨跌见上" if len(rows) == 1 else
                "市场基调：宽基涨跌分化" if any(row["change_pct"] > 0 for row in rows) and any(
            row["change_pct"] < 0 for row in rows) else "市场基调：宽基整体偏弱" if all(
            row["change_pct"] < 0 for row in rows) else "市场基调：宽基整体偏强")
    return [f"市场（{time}）：{detail}", lead]


def transition(market: dict[str, Any], previous: dict[str, Any],
               now: datetime) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One alert per verified episode; a missing quote cannot recover it."""
    day = now.astimezone(SHANGHAI).date().isoformat()
    old = dict(previous) if previous.get("trade_date") == day else {}
    rows = market.get("rows") or []
    if not rows:
        return old or {"trade_date": day, "level": 0}, None
    level = risk_level(market)
    state = {**old, "trade_date": day, "level": level,
             "indices": {row["name"]: row["change_pct"] for row in rows}}
    if not old:
        # Deployment into an existing drop establishes a baseline.
        return state | {"baseline_only": True, "recovery_count": 0}, None
    if level == 0:
        count = int(old.get("recovery_count") or 0) + 1
        state["recovery_count"] = count
        if count >= 2:
            state["armed"] = True
        return state, None
    state["recovery_count"] = 0
    prior = old.get("accepted_indices") or (old.get("indices") if old.get("baseline_only") else {}) or {}
    deeper = any(row["name"] in prior and
                 row["change_pct"] <= prior[row["name"]] - 1.0
                 for row in rows)
    fresh = bool(old.get("armed") and old.get("level", 0) == 0)
    escalated = old.get("accepted_level") is not None and level > old["accepted_level"]
    if not (fresh or (prior and (deeper or escalated))):
        return state, None
    last = _stamp(old.get("accepted_at"))
    if last and now - last < timedelta(minutes=30):
        return state, None
    episode = int(old.get("episode") or 0) + 1
    return state, {"policy_version": VERSION, "level": level,
                   "episode": episode, "trade_date": day,
                   "indices": {row["name"]: row["change_pct"] for row in rows},
                   "as_of": min(row["as_of"] for row in rows)}


def accepted(state: dict[str, Any], event: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {**state, "accepted_indices": event["indices"],
            "accepted_level": event["level"], "accepted_at": now.isoformat(timespec="seconds"),
            "episode": event["episode"], "armed": False}


def report_reference(market: dict[str, Any], day: str, now: datetime,
                     account: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Bound an accepted report's public index baseline before persistence."""
    if not isinstance(market, dict) or market.get("version") != VERSION:
        return None
    indices = {}
    stamps = []
    for row in market.get("rows") or []:
        if not isinstance(row, dict) or row.get("name") not in INDEX_CODES:
            continue
        stamp = _stamp(row.get("as_of"))
        pct = _number(row.get("change_pct"))
        if (stamp and stamp.date().isoformat() == day and stamp <= now + timedelta(minutes=1)
                and now - stamp <= timedelta(hours=7) and pct is not None
                and -25 <= pct <= 25 and row.get("source") == "tencent_full_index_quote"
                and row.get("time_source") == "provider"):
            indices[row["name"]] = pct
            stamps.append(stamp)
    safe_account = {}
    if isinstance(account, dict) and account.get("status") == "current":
        fields = ("total", "covered", "down", "risk_observations")
        if all(account.get(key) is None or
               (type(account[key]) is int and 0 <= account[key] <= 10000) for key in fields):
            safe_account = {key: account.get(key) for key in fields}
            safe_account["status"] = "current"
    if not indices and not safe_account:
        return None
    return {"version": VERSION, "trade_date": day, "indices": indices,
            "level": risk_level({"rows": [
                {"name": name, "change_pct": pct} for name, pct in indices.items()]}),
            "account": safe_account,
            "market_as_of": min(stamps).isoformat(timespec="seconds") if stamps else None,
            "accepted_at": now.isoformat(timespec="seconds")}


def newer_reference(state: dict[str, Any], report: dict[str, Any], day: str) -> dict[str, Any]:
    """Merge only an accepted same-day report newer than the monitor baseline."""
    old = state if state.get("trade_date") == day else {}
    if not isinstance(report, dict) or report.get("trade_date") != day:
        return old
    report_at = _stamp(report.get("accepted_at"))
    old_at = _stamp(old.get("accepted_at"))
    if not report_at or (old_at and report_at <= old_at):
        return old
    indices = report.get("indices") or {}
    if not isinstance(indices, dict) or not indices or any(
            name not in INDEX_CODES or _number(pct) is None for name, pct in indices.items()):
        return old
    return {**old, "trade_date": day, "accepted_indices": indices,
            "accepted_level": report.get("level", 0),
            "accepted_at": report_at.isoformat(timespec="seconds"),
            "baseline_only": False}


def account_risk(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Count only same-batch covered holdings, including funds and ETFs."""
    from application.market_prices import current_day_price
    day = str((snapshot.get("trading_day") or {}).get("date") or "")
    captured = _stamp((snapshot.get("as_of") or {}).get("captured_at"))
    holdings = snapshot.get("holdings") or {}
    if holdings.get("status") != "complete" or not captured or captured.date().isoformat() != day:
        return {"status": "unavailable"}
    rows = holdings.get("rows") or []
    quotes = {str(row.get("symbol") or "").zfill(6): row
              for row in (snapshot.get("quotes") or {}).get("rows") or []
              if isinstance(row, dict)}
    covered = down = 0
    largest = None
    estimate = 0.0
    estimate_complete = bool(rows) and len(rows) == holdings.get("count")
    largest_impact = None
    for item in rows:
        symbol = str(item.get("symbol") or item.get("code") or "").zfill(6)
        quote = quotes.get(symbol) or {}
        pct = _number(quote.get("change_pct"))
        price = current_day_price(quote, captured)
        if pct is None or price is None:
            estimate_complete = False
            continue
        covered += 1
        if pct < 0:
            down += 1
            if largest is None or pct < largest["change_pct"]:
                largest = {"symbol": symbol, "name": str(item.get("name") or quote.get("name") or "")[:24],
                           "change_pct": round(pct, 2)}
        quantity = _number(item.get("quantity"))
        last_close = _number(quote.get("last_close"))
        if (item.get("asset_class") not in {"stock", "fund_or_etf_or_lof"}
                or quantity is None or quantity <= 0 or last_close is None
                or last_close <= 0):
            estimate_complete = False
        else:
            impact = quantity * (price - last_close)
            estimate += impact
            if largest_impact is None or impact < largest_impact["amount_cny"]:
                largest_impact = {"symbol": symbol,
                    "name": str(item.get("name") or quote.get("name") or "")[:24],
                    "amount_cny": round(impact, 2)}
    plans = snapshot.get("trade_plans") or {}
    authority = plans.get("holding_actions_authority") or {}
    observed = None
    if authority.get("status") == "current":
        observed = sum(1 for row in plans.get("holding_actions") or []
                       if isinstance(row, dict)
                       and (row.get("requested_action") or row.get("action")) in {"sell", "reduce"}
                       and row.get("quote_as_of") ==
                           (quotes.get(str(row.get("symbol") or "").zfill(6)) or {}).get("as_of"))
    total = holdings.get("count")
    total = total if type(total) is int and total >= len(rows) else len(rows)
    return {"status": "current", "total": total, "covered": covered,
            "down": down, "risk_observations": observed, "largest_decline": largest,
            "day_change_estimate_cny": round(estimate, 2) if estimate_complete else None,
            "largest_impact": largest_impact if estimate_complete else None}
