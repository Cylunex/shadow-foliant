"""Bounded, row-oriented scheduled QQ rendering from one protected snapshot."""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any

from notify.plain_language import plain_text

MAX_LINES = 24
MAX_CHARS = 1500


def _code(row: dict[str, Any]) -> str:
    value = str(row.get("symbol") or row.get("code") or "").strip()
    return value.zfill(6) if value.isdigit() and len(value) <= 6 else ""


def _field(value: Any, limit: int = 28) -> str:
    value = " ".join(str(value or "").split())
    value = re.sub(r"[|｜<>]", " ", value)
    return plain_text(value, limit)


def render(snapshot: dict[str, Any]) -> tuple[str, str]:
    day = str((snapshot.get("trading_day") or {}).get("date") or "")
    quotes = snapshot.get("quotes") or {}
    quote_rows = {_code(row): row for row in quotes.get("rows") or []
                  if isinstance(row, dict) and _code(row)}
    captured = str(quotes.get("captured_at") or (snapshot.get("as_of") or {}).get("captured_at") or "")
    try:
        captured_at = datetime.fromisoformat(captured)
        if captured_at.tzinfo is None or captured_at.date().isoformat() != day:
            captured_at = None
    except ValueError:
        captured_at = None
    quote_times: list[datetime] = []

    def current_quote(symbol: str) -> str:
        row = quote_rows.get(symbol) or {}
        try:
            price = float(row.get("price"))
            as_of = datetime.fromisoformat(str(row.get("as_of") or ""))
        except (TypeError, ValueError):
            return "现价待核验"
        if (not math.isfinite(price) or price <= 0 or not captured_at
                or not as_of.tzinfo or as_of.date().isoformat() != day
                or as_of > captured_at
                or row.get("freshness") not in {"actionable", "closing_current"}):
            return "现价待核验"
        quote_times.append(as_of)
        return f"¥{price:.2f}"

    def name(symbol: str, names: set[str]) -> str:
        value = _field((quote_rows.get(symbol) or {}).get("name"))
        if value and value != symbol:
            names.add(value)
        return next(iter(names)) if len(names) == 1 else "名称待核验"

    picks: dict[str, dict[str, Any]] = {}
    for source, section, key in (("正式", snapshot.get("formal_selection") or {}, "formal_top5"),
                                 ("独立量化", snapshot.get("independent_selection") or {}, "top5")):
        if section.get("status") != "complete":
            continue
        for row in (section.get(key) or [])[:5]:
            if not isinstance(row, dict) or not (symbol := _code(row)):
                continue
            entry = picks.setdefault(symbol, {"sources": [], "names": set()})
            if source not in entry["sources"]:
                entry["sources"].append(source)
            row_name = _field(row.get("name"))
            if row_name and row_name != symbol:
                entry["names"].add(row_name)

    selection_rows = []
    for symbol, entry in picks.items():
        price = current_quote(symbol)
        selection_rows.append(f"{symbol}｜{name(symbol, entry['names'])}｜{price}｜{'+'.join(entry['sources'])}")

    plans = snapshot.get("trade_plans") or {}
    formal = snapshot.get("formal_selection") or {}
    post_close = (snapshot.get("post_close_review") or {}).get("due") is True
    if post_close:
        review = snapshot.get("holdings_review") or {}
        action_rows = review.get("rows") or [] if review.get("status") in {"complete", "degraded"} else []
    else:
        authority = plans.get("holding_actions_authority") or {}
        action_rows = plans.get("holding_actions") or [] if authority.get("status") == "current" else []
    operations = []
    seen_actions = set()
    for row in action_rows:
        if not isinstance(row, dict) or not (symbol := _code(row)) or symbol in seen_actions:
            continue
        final = str(row.get("action") or "")
        requested = str(row.get("requested_action") or final)
        guarded = requested in {"sell", "reduce"} and final == "hold"
        if final not in {"buy", "add", "sell", "reduce"} and not guarded:
            continue
        seen_actions.add(symbol)
        price = current_quote(symbol)
        try:
            valid_until = datetime.fromisoformat(str(row.get("plan_valid_until") or ""))
            sellable = float(row.get("sellable_quantity") or 0)
        except (TypeError, ValueError):
            valid_until, sellable = None, 0
        executable = bool(
            not post_close and price != "现价待核验" and captured_at
            and row.get("quote_as_of") == (quote_rows.get(symbol) or {}).get("as_of")
            and row.get("price") == (quote_rows.get(symbol) or {}).get("price")
            and (plans.get("holding_actions_authority") or {}).get("critical_alert_gate") == "ready"
            and row.get("price_actionable") is True
            and row.get("quote_time_source") == "provider"
            and bool(row.get("quote_provider"))
            and row.get("plan_available") is True
            and row.get("plan_adjustment") == "qfq"
            and row.get("quote_adjustment") == "qfq"
            and "trade_plan" in str(row.get("price_basis") or "")
            and row.get("selection_run_id") == formal.get("run_id")
            and row.get("plan_as_of") == formal.get("market_as_of")
            and row.get("tradeable") is True
            and row.get("sellability_source") == "broker"
            and sellable > 0
            and str(row.get("sellability_as_of") or "")[:10] == day
            and valid_until and valid_until.tzinfo and valid_until >= captured_at
        )
        if guarded:
            action, reason = "受保护观察", "原始风险触发；组合保护后保持观察"
        elif final in {"sell", "reduce"}:
            action = {"sell": "卖出", "reduce": "减仓"}[final] + ("" if executable else "观察")
            raw_reason = str(row.get("reason") or "")
            reason = ("触及止损" if "止损" in raw_reason else
                      "触及目标价" if "目标" in raw_reason or "止盈" in raw_reason else
                      "组合风险信号" if row.get("decision_source") == "portfolio_risk" else
                      "计划风险信号")
            if not executable:
                reason += "；执行条件待核验"
        else:
            action, reason = ("买入观察" if final == "buy" else "加仓观察"), "候选信号；买入条件待核验"
        row_name = _field(row.get("name"))
        names = {row_name} if row_name and row_name != symbol else set()
        operations.append(f"{symbol}｜{name(symbol, names)}｜{price}｜{action}｜{reason}")

    time_label = min(quote_times).strftime("%H:%M") if quote_times else (
        captured_at.strftime("%H:%M") if captured_at else "待核验")
    lines = [f"选股（{day or '日期待核验'} {'收盘行情' if post_close else '行情'}{time_label}）",
             "编号｜名称｜现价｜来源", *(selection_rows or ["当期无可用选股候选"])]
    if operations:
        lines.extend(["操作观察", "编号｜名称｜现价｜操作｜原因", *operations])
    else:
        lines.append("操作：当期无须单列的风险动作")
    selected = []
    for index, line in enumerate(lines):
        remaining = len(lines) - index - 1
        suffix = f"另有{remaining}条未展示" if remaining else ""
        candidate = selected + [line] + ([suffix] if suffix else [])
        if len(candidate) > MAX_LINES or len("\n".join(candidate)) > MAX_CHARS:
            break
        selected.append(line)
    omitted = len(lines) - len(selected)
    if omitted:
        selected.append(f"另有{omitted}条未展示")
    market_label = "收盘行情" if post_close else "行情"
    return f"选股与操作｜{day[5:] or '当期'} {time_label}{market_label}", "\n".join(selected)
