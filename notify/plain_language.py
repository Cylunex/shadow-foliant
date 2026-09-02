"""面向即时通知的通俗化工具。

本模块只压缩和翻译已有结论，不根据关键词生成新的交易判断。市场方向和仓位动作必须由
业务规则或已校验的分析结果显式传入。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple


_DIRECTION = {
    "看涨": ("🔴", "看涨"),
    "看跌": ("🟢", "看跌"),
    "震荡": ("⚪", "震荡"),
}
_ACTIONS = {"加仓", "减仓", "不动"}

_PLAIN_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    (r"破\s*MA\s*(20|60)", r"跌破\1日均线"),
    (r"MA\s*250|250\s*日均线|年线", "长期均线"),
    (r"MA\s*120|120\s*日均线|半年线", "半年均线"),
    (r"MA\s*60", "60日均线"),
    (r"MA\s*20", "20日均线"),
    (r"VaR\s*95(?:%|％)?", "短期风险"),
    (r"ATR(?:20)?(?:分位)?", "波动"),
    (r"缠论[一二三]卖", "走势转弱"),
    (r"缠论[一二三]买", "走势转强"),
    (r"多头排列", "走势偏强"),
    (r"空头排列", "走势偏弱"),
    (r"超额收益", "比大盘强"),
    (r"回撤", "下跌"),
    (r"风险敞口", "持仓风险"),
    (r"置信度", "把握"),
)

_DECORATION = re.compile(r"^[\s━─═=*_#•·-]+$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")


def normalize_direction(value: object) -> str:
    text = str(value or "").strip()
    has_up = "涨" in text or "偏强" in text
    has_down = "跌" in text or "偏弱" in text
    if has_up and has_down:
        return "震荡"
    if has_down:
        return "看跌"
    if has_up:
        return "看涨"
    return "震荡"


def normalize_action(value: object) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if ("减" in text or "卖" in text or "清仓" in text
            or lowered in {"reduce", "sell", "avoid"}):
        return "减仓"
    if "加" in text or "买" in text or lowered in {"buy", "add", "strong_buy"}:
        return "加仓"
    return "不动"


def plain_text(value: object, limit: int = 90) -> str:
    """去 Markdown 装饰并把常见指标翻译为人话。"""
    text = _HEADING.sub("", str(value or "").strip())
    text = re.sub(r"[*`]", "", text)
    text = re.sub(r"(?<!\w)_+(.+?)_+(?!\w)", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    for pattern, replacement in _PLAIN_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = text.strip(" ：:；;，,")
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip("，；。 ") + "…"
    return text


def build_market_message(
    *,
    label: str,
    direction: object,
    action: object,
    market: object = "",
    holdings: object = "",
    reason: object = "",
    as_of: object = "",
) -> Tuple[str, str]:
    """构造只回答方向和动作的短消息，返回 ``(title, content)``。"""
    direction_text = normalize_direction(direction)
    action_text = normalize_action(action)
    icon, _ = _DIRECTION[direction_text]
    title = f"{icon} {label}：{direction_text}｜{action_text}"
    lines = [f"操作：{action_text}"]
    if plain_text(market):
        lines.append(f"大盘：{plain_text(market, 110)}")
    if plain_text(holdings):
        lines.append(f"持仓：{plain_text(holdings, 130)}")
    if plain_text(reason):
        lines.append(f"原因：{plain_text(reason, 110)}")
    if plain_text(as_of):
        lines.append(f"时间：{plain_text(as_of, 60)}")
    return title, "\n".join(lines)


def build_portfolio_message(
    *,
    label: str,
    signal: object,
    sell_rows: Iterable[dict] = (),
    buy_rows: Iterable[dict] = (),
    market: object = "",
    as_of: object = "",
    item_limit: int = 4,
) -> Tuple[str, str]:
    """把组合规则和个股信号合成一条“涨跌 + 加减仓”通知。"""
    data = signal if isinstance(signal, dict) else {}
    try:
        median = float(data.get("median_change"))
    except (TypeError, ValueError):
        median = 0.0
    direction = "看涨" if median >= 0.3 else ("看跌" if median <= -0.3 else "震荡")
    action = normalize_action(data.get("action") or data.get("action_cn"))
    # Risk wins for conflicting rows. Counts must describe the same deduplicated
    # candidates we actually display, not raw scanner hits.
    def unique(rows):
        output = {}
        for row in rows or ():
            code = str(row.get("code") or "")
            if code:
                output.setdefault(code, row)
        return list(output.values())

    sells = unique(sell_rows)
    sell_codes = {str(row["code"]) for row in sells}
    buys = unique(row for row in buy_rows or ()
                  if str(row.get("code") or "") not in sell_codes
                  and not row.get("sell_score"))
    holding_summary = (
        f"减仓关注{len(sells)}只、加仓候选{len(buys)}只（下列重点）"
        if sells or buys else "没有必须处理的，先不动"
    )
    title, body = build_market_message(
        label=label,
        direction=direction,
        action=action,
        market=market,
        holdings=holding_summary,
        reason=data.get("reason") or "数据不足时保持仓位",
        as_of=as_of,
    )
    lines = body.splitlines()
    if action == "加仓" and not buys:
        lines[0] = "操作：盘面允许加仓，但暂无合适个股，先等"
    # Reports are delivered with an eight-line budget. Put the primary action
    # first, but reserve a slot for the other side so risks are never hidden.
    limit = min(max(0, int(item_limit)), max(0, 8 - len(lines)))
    groups = [("加仓", buys), ("减仓", sells)] if action == "加仓" else [
        ("减仓", sells), ("加仓", buys)
    ]
    selected = []
    if limit and groups[0][1] and groups[1][1]:
        primary_limit = max(1, limit - 1)
        selected += [(groups[0][0], row) for row in groups[0][1][:primary_limit]]
        selected += [(groups[1][0], groups[1][1][0])] if limit > 1 else []
    for row_action, rows in groups:
        for row in rows:
            if len(selected) >= limit:
                break
            if not any(str(item[1]["code"]) == str(row["code"]) for item in selected):
                selected.append((row_action, row))
    for row_action, row in selected:
        try:
            change = float(row.get("change"))
            move = f"涨{change:.1f}%" if change > 0 else (
                f"跌{abs(change):.1f}%" if change < 0 else "没涨没跌"
            )
        except (TypeError, ValueError):
            move = "涨跌待更新"
        reasons = row.get("sell_reasons") if row_action == "减仓" else None
        reason = (reasons or [row.get("buy_reason") or "按当前信号处理"])[0]
        name = plain_text(row.get("name") or row["code"], 20)
        lines.append(f"{row_action}：{name}｜{move}｜{plain_text(reason, 38)}")
    return title, "\n".join(lines)


def compact_notification(category: str, content: object,
                         *, max_lines: Optional[int] = None,
                         max_chars: Optional[int] = None) -> str:
    """压缩即时消息；存档正文保持原样。"""
    raw = str(content or "").strip()
    if category == "archive" or not raw:
        return raw
    line_limit = max_lines or (10 if category in {"alert", "system_error"} else 8)
    char_limit = max_chars or (1200 if category in {"alert", "system_error"} else 900)
    lines = []
    seen = set()
    for original in raw.splitlines():
        candidate = original.strip()
        if not candidate or _DECORATION.fullmatch(candidate):
            continue
        candidate = plain_text(candidate, 180)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        lines.append(candidate)
        if len(lines) >= line_limit:
            break
    result = "\n".join(lines)
    if len(result) > char_limit:
        result = result[: max(1, char_limit - 1)].rstrip("，；。 \n") + "…"
    return result
