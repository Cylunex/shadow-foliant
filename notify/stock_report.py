"""Readable stock reports. Presentation only; never infer a trading signal."""
from __future__ import annotations

import math
import re

from application.scheduled_report_contract import (
    REPORT_MAX_BYTES, REPORT_MAX_CHARS, REPORT_MAX_LINES,
)


def text(value: object, limit: int = 100) -> str:
    from notify.plain_language import plain_text
    return plain_text(" ".join(str(value or "").split()), limit)


def price(value: object) -> str:
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return f"{number:.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        pass
    return "待核验"


def is_report(body: str) -> bool:
    return body.startswith(("【A股参考｜", "【基金定投｜"))


def validate(body: str) -> str:
    # Fail explicitly instead of silently cutting off a risk or data warning.
    if (len(body) > REPORT_MAX_CHARS or len(body.encode("utf-8")) > REPORT_MAX_BYTES
            or len(body.splitlines()) > REPORT_MAX_LINES):
        raise ValueError("stock_report_exceeds_budget")
    return body


def report(label: str, as_of: object, sections: list, notes=(), *, fund=False) -> str:
    stamp = text(as_of, 32).replace("T", " ")
    match = re.match(r"\d{4}-(\d{2})-(\d{2}) (\d{2}:\d{2})", stamp)
    if match:
        stamp = f"{int(match[1])}/{int(match[2])} {match[3]}"
    lines = [f"【{'基金定投' if fund else 'A股参考'}｜{text(label, 16)}｜{stamp or '时点待核验'}】"]
    for heading, rows in sections:
        lines.extend(["", heading, *[str(row) for row in rows if row]])
    if notes:
        lines.extend(["", *notes])
    return validate("\n".join(lines))


def selection_report(rows, *, held_codes=(), as_of="", market_as_of="", names=None,
                     reference=None, data_note="") -> str:
    held = {str(code)[-6:] for code in held_codes}
    selected, seen = [], set()
    for row in rows or []:
        code = str(row.get("code") or row.get("symbol") or "")[-6:]
        if not re.fullmatch(r"\d{6}", code) or code in held or code in seen or code.startswith("688"):
            continue
        seen.add(code)
        name = text(row.get("name") or (names or {}).get(code) or code, 16)
        reason = text(row.get("reason") or row.get("primary_strategy_name"), 55)
        selected.append(f"• {name}（{code}）：参考价 {price(row.get('price'))}。"
                        + (f"{reason}。" if reason else "入选项目候选，具体买点待核验。"))
        if len(selected) == 3:
            break
    notes = ["项目候选，仅作观察；排名不代表买入时机。"]
    if reference is not None:
        notes.append(f"补充参考：问财{int(reference.get('ready_groups') or 0)}/5组已核验；完整名单与来源见存档。")
    if data_note:
        notes.append("数据说明：" + text(data_note, 80))
    return report("选股观察", as_of, [
        ("新选股观察（非持仓、无688）", selected or ["本轮暂无符合条件的新候选。"]),
        ("参考口径", [f"选股依据截至 {text(market_as_of, 20) or '待核验'}；报价为筛选时参考价。"]),
    ], notes)
