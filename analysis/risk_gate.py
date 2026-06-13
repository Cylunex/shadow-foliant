# -*- coding: utf-8 -*-
"""下单前风控门(fail-closed)—— 借鉴 Vibe-Trading order guard / mandate enforcement。

AI 盯盘(smart_monitor)自动下单前必须过这道门:**任一硬约束不满足 → 拒单**(fail-closed,
宁可不交易也不越界)。专为 A 股加了涨跌停/ST 等本土约束。配置可由 .env 覆盖(RISK_GATE_*)。

硬约束(命中即拒):
  · 排除名单(RISK_GATE_EXCLUDE,逗号分隔代码)
  · ST/退市风险(名称含 ST/*ST/退)—— 除非 RISK_GATE_ALLOW_ST=true
  · 追涨停(当日涨幅 ≥ 阈值,默认 9.7%;创业板/科创 19.7%)—— 买不到且风险高
  · 单票超限(买入后该票占总资产 > RISK_GATE_SINGLE_MAX_PCT,默认 25)
  · 总仓超限(买入后总持仓占比 > RISK_GATE_TOTAL_MAX_PCT,默认 95)
  · 日内下单次数超限(RISK_GATE_MAX_DAILY_ORDERS,默认 20)
  · 单笔金额超限(RISK_GATE_MAX_SINGLE_ORDER,默认不限)
缺数据的约束**跳过并告警**(不因取不到数据误杀),但 ST/排除/涨停/次数 这些有数据的硬约束严格执行。

用法:
    from risk_gate import check_buy
    v = check_buy(code, name, price, amount, change_pct=..., total_assets=...,
                  cur_position_value=..., total_position_value=..., today_order_count=...)
    if not v["approved"]: 拒单, 看 v["blocks"]
"""
from __future__ import annotations

import os
from typing import List, Optional

import _bootstrap  # noqa: F401


def _cfg_f(key, default):
    try:
        v = os.getenv(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _is_growth_or_star(code: str) -> bool:
    code = str(code)
    return code.startswith(("300", "301", "688", "689"))  # 创业板/科创板 ±20%


def check_buy(code: str, name: str = "", price: float = 0, amount: float = 0, *,
              change_pct: Optional[float] = None,
              total_assets: Optional[float] = None,
              cur_position_value: float = 0.0,
              total_position_value: float = 0.0,
              today_order_count: int = 0,
              config: Optional[dict] = None) -> dict:
    """买入风控校验。返回 {approved, blocks:[硬约束理由], warnings:[], detail}。"""
    c = {
        "exclude": (os.getenv("RISK_GATE_EXCLUDE", "") or "").replace("，", ",").split(","),
        "allow_st": (os.getenv("RISK_GATE_ALLOW_ST", "false") or "").lower() in ("1", "true", "yes", "on"),
        "single_max_pct": _cfg_f("RISK_GATE_SINGLE_MAX_PCT", 25.0),
        "total_max_pct": _cfg_f("RISK_GATE_TOTAL_MAX_PCT", 95.0),
        "max_daily_orders": int(_cfg_f("RISK_GATE_MAX_DAILY_ORDERS", 20)),
        "max_single_order": _cfg_f("RISK_GATE_MAX_SINGLE_ORDER", 0),  # 0=不限
        "limit_pct": _cfg_f("RISK_GATE_LIMIT_PCT", 9.7),
        "limit_pct_star": _cfg_f("RISK_GATE_LIMIT_PCT_STAR", 19.7),
    }
    if config:
        c.update(config)
    exclude = {x.strip() for x in c["exclude"] if x.strip()}

    blocks: List[str] = []
    warnings: List[str] = []

    # 排除名单
    if str(code) in exclude:
        blocks.append(f"{code} 在排除名单内")
    # ST/退市
    if not c["allow_st"] and any(t in str(name).upper() for t in ("ST", "退")):
        blocks.append(f"{name} 为 ST/退市风险股(RISK_GATE_ALLOW_ST 可放开)")
    # 追涨停
    if change_pct is not None:
        lim = c["limit_pct_star"] if _is_growth_or_star(code) else c["limit_pct"]
        if change_pct >= lim:
            blocks.append(f"当日已涨 {change_pct:.1f}%(≥{lim}%)接近涨停,不追高")
    else:
        warnings.append("无当日涨幅数据,跳过涨停校验")
    # 日内下单次数
    if today_order_count >= c["max_daily_orders"]:
        blocks.append(f"日内下单已达 {today_order_count} 次(上限 {c['max_daily_orders']})")
    # 单笔金额
    if c["max_single_order"] and amount > c["max_single_order"]:
        blocks.append(f"单笔金额 {amount:.0f} 超上限 {c['max_single_order']:.0f}")
    # 仓位约束(需总资产)
    if total_assets and total_assets > 0:
        single_after = (cur_position_value + amount) / total_assets * 100
        if single_after > c["single_max_pct"]:
            blocks.append(f"买入后单票占比 {single_after:.1f}% 超上限 {c['single_max_pct']:.0f}%")
        total_after = (total_position_value + amount) / total_assets * 100
        if total_after > c["total_max_pct"]:
            blocks.append(f"买入后总仓位 {total_after:.1f}% 超上限 {c['total_max_pct']:.0f}%")
    else:
        warnings.append("无总资产数据,跳过仓位上限校验")

    approved = not blocks
    detail = ("✅ 风控通过" if approved else "🚫 风控拒单:" + "；".join(blocks))
    if warnings:
        detail += "（" + "；".join(warnings) + "）"
    return {"approved": approved, "blocks": blocks, "warnings": warnings, "detail": detail}


def check_sell(code: str, name: str = "", *, can_sell: int = 0,
               change_pct: Optional[float] = None, config: Optional[dict] = None) -> dict:
    """卖出风控:A股跌停基本卖不出(告警不拒,因止损可能挂单);无可卖份额则拒。"""
    blocks, warnings = [], []
    if can_sell <= 0:
        blocks.append("无可卖份额(T+1 未解禁或已清仓)")
    if change_pct is not None:
        lim = -(_cfg_f("RISK_GATE_LIMIT_PCT_STAR", 19.7) if _is_growth_or_star(code) else _cfg_f("RISK_GATE_LIMIT_PCT", 9.7))
        if change_pct <= lim:
            warnings.append(f"当日已跌 {change_pct:.1f}% 接近跌停,可能无法成交")
    approved = not blocks
    return {"approved": approved, "blocks": blocks, "warnings": warnings,
            "detail": ("✅ 可卖" if approved else "🚫 " + "；".join(blocks)) + (("（" + "；".join(warnings) + "）") if warnings else "")}


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("=== 风控门自检 ===")
    print("正常买入:", check_buy("600519", "贵州茅台", 1700, 17000, change_pct=1.2,
                              total_assets=200000, cur_position_value=0, total_position_value=100000)["detail"])
    print("追涨停:", check_buy("600519", "贵州茅台", 1700, 17000, change_pct=9.9)["detail"])
    print("ST股:", check_buy("000xxx", "*ST荣华", 3, 3000, change_pct=1)["detail"])
    print("单票超限:", check_buy("600519", "茅台", 1700, 60000, change_pct=1,
                              total_assets=200000, cur_position_value=10000, total_position_value=50000)["detail"])
    print("创业板涨幅15%(未到20%涨停):", check_buy("300750", "宁德时代", 200, 20000, change_pct=15)["detail"])
