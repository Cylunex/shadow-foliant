"""Bounded, row-oriented scheduled QQ rendering from one protected snapshot."""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any

from notify.plain_language import plain_text
from application.scheduled_report_contract import (REPORT_MAX_BYTES, REPORT_MAX_CHARS,
    REPORT_MAX_LINES, REPORT_MAX_ORIGINAL_LINES, REPORT_MAX_PARTS)

MAX_LINES = REPORT_MAX_LINES
MAX_CHARS = REPORT_MAX_CHARS


def _fits(body: str) -> bool:
    return (len(body) <= MAX_CHARS and len(body.encode("utf-8")) <= REPORT_MAX_BYTES
            and len(body.splitlines()) <= MAX_LINES)


def render_parts(title: str, body: str) -> list[tuple[str, str]]:
    """Pack complete report lines, in priority order, into one or more QQ messages."""
    if _fits(body):
        return [(title, body)]
    lines = body.splitlines()
    if len(lines) > REPORT_MAX_ORIGINAL_LINES:
        raise ValueError("qq_report_too_many_lines")
    # Reserve the widest part marker before packing, so numbering cannot make a
    # previously fitted part overflow. The same report title is kept on each part.
    groups: list[list[str]] = []
    current: list[str] = []
    marker = f"{title}｜{REPORT_MAX_PARTS}/{REPORT_MAX_PARTS}"
    in_selection = False
    for line in lines:
        if line.startswith("选股（"):
            in_selection = True
        candidate = "\n".join([marker, *current, line])
        if not _fits(candidate):
            if not current:
                raise ValueError("qq_report_line_exceeds_budget")
            groups.append(current)
            header = ("编号｜名称｜现价｜来源" if in_selection else
                      "编号｜名称｜现价｜操作｜原因")
            current = ([header] if re.match(r"^\d{6}｜", line) else []) + [line]
            if not _fits("\n".join([marker, *current])):
                raise ValueError("qq_report_line_exceeds_budget")
        else:
            current.append(line)
    if current:
        groups.append(current)
    if len(groups) > REPORT_MAX_PARTS:
        raise ValueError("qq_report_exceeds_part_budget")
    total = len(groups)
    return [(f"{title}｜{index}/{total}",
             "\n".join([f"{title}｜{index}/{total}", *group]))
            for index, group in enumerate(groups, 1)]


def _code(row: dict[str, Any]) -> str:
    value = str(row.get("symbol") or row.get("code") or "").strip()
    return value.zfill(6) if value.isdigit() and len(value) <= 6 else ""


def _field(value: Any, limit: int = 28) -> str:
    value = " ".join(str(value or "").split())
    value = re.sub(r"[|｜<>]", " ", value)
    return plain_text(value, limit)


def _count(value: Any) -> int:
    return min(value, 9999) if type(value) is int and value >= 0 else 0


def render(snapshot: dict[str, Any], *, include_selection: bool = True,
           selection_updated: bool = False) -> tuple[str, str]:
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
    if include_selection:
        from application.scheduled_pick_policy import SOURCES
        for source, section_name, key in SOURCES:
            section = snapshot.get(section_name) or {}
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
    operations: list[tuple[tuple[int, float, str], str]] = []
    protected_counts = {"sell": 0, "reduce": 0}
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
        raw_reason = str((row.get("requested_reason") if guarded else row.get("reason")) or "")
        hard_risk = (row.get("requested_source") if guarded else row.get("decision_source")) == "hard_risk"
        if guarded:
            protected_counts[requested] += 1
            if not hard_risk:
                continue
            action, reason = "止损受保护观察", "原始止损触发；组合保护后保持观察；执行未核验"
        elif final in {"sell", "reduce"}:
            action = {"sell": "卖出", "reduce": "减仓"}[final] + ("" if executable else "观察")
            reason = ("触及止损" if hard_risk or "止损" in raw_reason else
                      "触及目标价" if "目标" in raw_reason or "止盈" in raw_reason else
                      "组合风险信号" if row.get("decision_source") == "portfolio_risk" else
                      "计划风险信号")
            if not executable:
                reason += "；执行条件待核验"
        else:
            action, reason = ("买入观察" if final == "buy" else "加仓观察"), "候选信号；买入条件待核验"
        row_name = _field(row.get("name"))
        names = {row_name} if row_name and row_name != symbol else set()
        try:
            impact = float(row.get("market_value") or row.get("position_value") or 0)
            impact = impact if math.isfinite(impact) and impact > 0 else 0
        except (TypeError, ValueError):
            impact = 0
        if not impact and price != "现价待核验":
            try:
                quantity = float(row.get("quantity") or 0)
                impact = quantity * float(price.removeprefix("¥")) if quantity > 0 else 0
            except (TypeError, ValueError):
                pass
        priority = (0 if executable and final in {"sell", "reduce"} and hard_risk else
                    1 if executable and final in {"sell", "reduce"} else
                    2 if hard_risk else
                    3 if final in {"sell", "reduce"} else 4)
        operations.append(((priority, -impact, symbol),
                           f"{symbol}｜{name(symbol, names)}｜{price}｜{action}｜{reason}"))

    time_label = min(quote_times).strftime("%H:%M") if quote_times else (
        captured_at.strftime("%H:%M") if captured_at else "待核验")
    lines = []
    if operations:
        lines.extend([f"持仓操作与风险观察（{day or '日期待核验'} {'收盘行情' if post_close else '行情'}{time_label}）",
                      "编号｜名称｜现价｜操作｜原因",
                      *(line for _, line in sorted(operations))])
    if any(protected_counts.values()):
        lines.append(f"组合保护后继续观察：原始卖出{protected_counts['sell']}只、减仓{protected_counts['reduce']}只；风险未解除")
    if selection_rows:
        if selection_updated:
            lines.append("选股更新：已核验候选或有效来源结果变化")
        lines.extend([f"选股（{day or '日期待核验'} {'收盘行情' if post_close else '行情'}{time_label}）",
                      "编号｜名称｜现价｜来源", *selection_rows])
    if post_close:
        review = snapshot.get("holdings_review") or {}
        next_plan = snapshot.get("next_session_plan") or {}
        if review.get("status") in {"complete", "degraded"}:
            reviewed = f"持仓复核{_count(review.get('reviewed_count'))}/{_count(review.get('count'))}"
        else:
            reviewed = "持仓复核待核验"
        if next_plan.get("status") in {"complete", "degraded"}:
            planning = f"次日计划{_count(next_plan.get('ready_count'))}/{_count(next_plan.get('count'))}可用"
        else:
            planning = "次日计划待核验"
        lines.append(f"盘后复盘：{reviewed}；{planning}。")
    if not lines:
        return "", ""
    market_label = "收盘行情" if post_close else "行情"
    title_label = "选股与操作" if selection_rows else "持仓操作与复盘" if post_close else "持仓操作"
    return f"{title_label}｜{day[5:] or '当期'} {time_label}{market_label}", "\n".join(lines)
