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


def _selection_labels(rows, names=None, *, limit=5) -> list[str]:
    labels, seen = [], set()
    for row in rows or []:
        code = str(row.get("code") or row.get("symbol") or "")
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            continue
        seen.add(code)
        name = text(row.get("name") or (names or {}).get(code), 8)
        labels.append(f"{name}（{code}）" if name else code)
        if len(labels) == limit:
            break
    return labels


def _wencai_sections(reference, names):
    from selection.wencai_notification import reference_top5, verified_picks
    from selection.wencai_query_contract import ORDER

    reference = reference or {}
    groups = reference.get("strategies") or {}
    lines = []
    for name in ORDER:
        group = groups.get(name) or {}
        status = group.get("status")
        if status in {"ready", "trial_verified"}:
            labels = _selection_labels(verified_picks(group), names)
            detail = "、".join(labels) or "无命中"
        elif status in {"trial_unverified", "degraded"}:
            detail = "条件或时点待核验"
        elif status == "pending":
            detail = ("盘前缓存无效" if reference.get("availability_reason") == "premarket_cache_invalid"
                      else "盘前缓存缺失")
        else:
            detail = "暂不可用"
        lines.append(f"{name}：{detail}")
    top5 = _selection_labels(reference_top5(reference), names)
    return [
        ("问财选股结果（五组各最多5只，仅供参考）", lines),
        ("问财 TOP5（项目汇总，非官方排名）", [
            "、".join(top5) or "暂无已核验候选。",
            "汇总已核验组前5只；优先多组命中，再按最佳组内名次、代码排序。",
        ]),
    ]


def selection_report(rows, *, held_codes=(), as_of="", market_as_of="", names=None,
                     reference=None, data_note="", top15_rows=()) -> str:
    held = {str(code)[-6:] for code in held_codes}
    selected, seen = [], set()
    for row in rows or []:
        code = str(row.get("code") or row.get("symbol") or "")[-6:]
        if not re.fullmatch(r"\d{6}", code) or code in held or code in seen or code.startswith("688"):
            continue
        seen.add(code)
        name = text(row.get("name") or (names or {}).get(code) or code, 8)
        reason = text(row.get("reason") or row.get("primary_strategy_name"), 24)
        selected.append(f"• {name}（{code}）：参考价 {price(row.get('price'))}。"
                        + (f"{reason}。" if reason else "入选项目候选，具体买点待核验。"))
        if len(selected) == 3:
            break
    notes = ["项目候选，仅作观察；排名不代表买入时机。"]
    if reference is not None:
        ready = sum(group.get("status") in {"ready", "trial_verified"}
                    for group in (reference.get("strategies") or {}).values())
        notes.append(f"问财{ready}/5组已核验；参考结果不参与正式排名。")
    if data_note:
        notes.append("数据说明：" + text(data_note, 80))
    top15 = _selection_labels(top15_rows, names, limit=15)
    top5 = _selection_labels(rows, names)
    return report("选股观察", as_of, [
        ("正式 TOP15（完整候选）", [
            "、".join(f"{j + 1}.{top15[j]}" for j in range(i, min(i + 3, len(top15))))
            for i in range(0, len(top15), 3)
        ] or ["本轮未产出。"]),
        ("正式 TOP5（最终优选）", ["、".join(top5) or "本轮未产出。"]),
        *_wencai_sections(reference, names),
        ("新选股观察（非持仓、无688）", selected or ["本轮暂无符合条件的新候选。"]),
        ("参考口径", [f"选股依据截至 {text(market_as_of, 20) or '待核验'}；报价为筛选时参考价。"]),
    ], notes)
