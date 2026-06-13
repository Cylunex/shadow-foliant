# -*- coding: utf-8 -*-
"""影子账户 · 交易行为诊断 —— 借鉴 Vibe-Trading shadow account 思路。

从**真实成交记录**(trade_records)FIFO 配对出"回合交易(round-trip)",反演你的交易行为,
诊断常见的散户行为偏差并给建议:
  · 处置效应(disposition effect):赚就跑、亏死扛 —— 盈利单 vs 亏损单的平均持有天数对比
  · 止损纪律:亏损单里有多少深亏(跌破 -X% 才割)、平均亏损幅度
  · 盈亏比 / 胜率:回合交易级别
  · 过度交易:月均交易笔数、换手、单票反复进出
诊断项格式同 portfolio_rules(passed/severity/detail/suggestion),产出报告 + 文本。

纯逻辑,数据只需 portfolio_db.get_trades();失败返回 {error} 不抛异常。
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, List, Optional

import _bootstrap  # noqa: F401

SELL = {"卖出", "卖", "sell", "S", "减仓"}


def _d(s) -> str:
    return str(s or "")[:10]


def _days(d1: str, d2: str) -> Optional[int]:
    try:
        return (datetime.strptime(_d(d2), "%Y-%m-%d") - datetime.strptime(_d(d1), "%Y-%m-%d")).days
    except Exception:
        return None


def _round_trips(trades: List[dict]) -> List[dict]:
    """FIFO 配对买卖 → 回合交易。每个回合:{code,name,buy_date,sell_date,hold_days,qty,
    buy_price,sell_price,pnl,pnl_pct}。只统计能配上买入的卖出。"""
    by_code: Dict[str, List[dict]] = defaultdict(list)
    for t in trades:
        code = str(t.get("code") or t.get("stock_code") or "").strip()
        if code:
            by_code[code].append(t)
    trips = []
    for code, ts in by_code.items():
        ts.sort(key=lambda x: _d(x.get("trade_time")))
        lots: deque = deque()  # 买入批次队列 [(date, qty_left, price, name)]
        for t in ts:
            ttype = str(t.get("trade_type") or "买入")
            qty = abs(int(float(t.get("quantity") or 0)))
            price = float(t.get("price") or 0)
            name = t.get("name") or t.get("stock_name") or code
            d = _d(t.get("trade_time"))
            if qty <= 0 or price <= 0:
                continue
            if ttype in SELL:
                remain = qty
                while remain > 0 and lots:
                    b = lots[0]
                    use = min(remain, b["qty"])
                    pnl = (price - b["price"]) * use
                    pnl_pct = round((price - b["price"]) / b["price"] * 100, 1) if b["price"] else None
                    trips.append({
                        "code": code, "name": name, "buy_date": b["date"], "sell_date": d,
                        "hold_days": _days(b["date"], d), "qty": use,
                        "buy_price": b["price"], "sell_price": price,
                        "pnl": round(pnl, 2), "pnl_pct": pnl_pct,
                    })
                    b["qty"] -= use
                    remain -= use
                    if b["qty"] <= 0:
                        lots.popleft()
                # 卖出多于持仓(历史遗留/做T)→ 剩余忽略
            else:
                lots.append({"date": d, "qty": qty, "price": price, "name": name})
    return trips


def _rule(key, name, passed, value, detail, suggestion="", severity=None):
    if severity is None:
        severity = "info" if passed or passed is None else "warn"
    return {"key": key, "name": name, "passed": passed, "value": value,
            "detail": detail, "suggestion": suggestion, "severity": severity}


def diagnose(trades: List[dict]) -> dict:
    """行为诊断报告。"""
    trips = _round_trips(trades)
    closed = [t for t in trips if t["pnl_pct"] is not None and t["hold_days"] is not None]
    if len(closed) < 3:
        return {"error": f"已完成回合交易过少({len(closed)}笔),需更多成交记录才能诊断", "n_trips": len(closed)}

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    win_rate = round(len(wins) / len(closed) * 100, 1)
    avg_win_pct = round(sum(t["pnl_pct"] for t in wins) / len(wins), 1) if wins else 0
    avg_loss_pct = round(sum(t["pnl_pct"] for t in losses) / len(losses), 1) if losses else 0
    avg_win_hold = round(sum(t["hold_days"] for t in wins) / len(wins), 1) if wins else 0
    avg_loss_hold = round(sum(t["hold_days"] for t in losses) / len(losses), 1) if losses else 0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss else None

    rules = []

    # 1. 处置效应:亏损单持有显著长于盈利单 = 赚就跑亏死扛(盈利单0天即做T小赚,更典型)
    if wins and losses:
        disp = avg_loss_hold > avg_win_hold * 1.3 + 1  # +1 容忍:避免持有期相近时误判
        rules.append(_rule("disposition", "处置效应", not disp,
                           f"盈利{avg_win_hold}天/亏损{avg_loss_hold}天",
                           f"盈利单平均持有 {avg_win_hold} 天、亏损单 {avg_loss_hold} 天",
                           "亏损单扛得明显比盈利单久(赚就跑、亏死扛),建议反过来:让利润奔跑、亏损早断" if disp else "",
                           "warn" if disp else "info"))

    # 2. 止损纪律:深亏(<-15%)回合占比
    deep = [t for t in losses if t["pnl_pct"] <= -15]
    deep_pct = round(len(deep) / len(closed) * 100, 1)
    rules.append(_rule("stoploss", "止损纪律", deep_pct <= 15, f"深亏(<-15%)回合占 {deep_pct}%",
                       f"{len(deep)} 笔深亏(平均亏损 {avg_loss_pct}%)",
                       "深亏回合偏多,说明止损执行不到位,建议设纪律止损" if deep_pct > 15 else "",
                       "alert" if deep_pct > 25 else ("warn" if deep_pct > 15 else "info")))

    # 3. 盈亏比
    if profit_factor is not None:
        rules.append(_rule("profit_factor", "盈亏比", profit_factor >= 1.2, profit_factor,
                           f"盈亏比 {profit_factor}(总盈/总亏)",
                           "盈亏比偏低,要么提高胜率、要么放大盈利单/收紧亏损单" if profit_factor < 1.2 else "",
                           "warn" if profit_factor < 1 else "info"))

    # 4. 胜率(信息项)
    rules.append(_rule("win_rate", "回合胜率", None, win_rate,
                       f"{len(closed)} 个回合,胜率 {win_rate}%(盈{len(wins)}/亏{len(losses)})"))

    # 5. 过度交易:月均回合数 + 短线(持有<5天)占比
    span_days = _days(min(t["buy_date"] for t in closed), max(t["sell_date"] for t in closed)) or 30
    months = max(span_days / 30.0, 1)
    per_month = round(len(closed) / months, 1)
    short = [t for t in closed if t["hold_days"] is not None and t["hold_days"] < 5]
    short_pct = round(len(short) / len(closed) * 100, 1)
    over = per_month > 30 or short_pct > 60
    rules.append(_rule("overtrading", "交易频率", not over,
                       f"月均{per_month}回合·短线(<5天)占{short_pct}%",
                       f"近 {round(months,1)} 个月 {len(closed)} 回合,月均 {per_month}、短线占 {short_pct}%",
                       "交易过于频繁,摩擦成本高且易情绪化,建议降低换手" if over else "",
                       "warn" if over else "info"))

    scored = [r for r in rules if r["passed"] is not None]
    passed_n = sum(1 for r in scored if r["passed"])
    score = round(passed_n / len(scored) * 100) if scored else None
    alerts = [r for r in rules if r["severity"] == "alert"]
    warns = [r for r in rules if r["severity"] == "warn"]
    return {
        "n_trips": len(closed), "win_rate": win_rate, "profit_factor": profit_factor,
        "avg_win_pct": avg_win_pct, "avg_loss_pct": avg_loss_pct,
        "avg_win_hold": avg_win_hold, "avg_loss_hold": avg_loss_hold,
        "rules": rules, "score": score, "passed_n": passed_n, "total_n": len(scored),
        "summary": f"交易行为 {score} 分;{passed_n}/{len(scored)} 项健康"
                   + (f",{len(alerts)} 项警报" if alerts else "")
                   + (f",{len(warns)} 项需改进" if warns else ""),
        # 最差/最佳回合(供参考)
        "worst": sorted(closed, key=lambda t: t["pnl"])[:3],
        "best": sorted(closed, key=lambda t: t["pnl"], reverse=True)[:3],
    }


def run_diagnose() -> dict:
    try:
        from portfolio_db import portfolio_db
        trades = portfolio_db.get_trades(limit=10000) or []
    except Exception as e:
        return {"error": f"读成交记录失败: {e}"}
    return diagnose(trades)


def format_text(rep: dict) -> str:
    if rep.get("error"):
        return f"🪞 交易行为诊断:{rep['error']}"
    L = [f"🪞 交易行为诊断 {rep['score']} 分 — {rep['summary']}",
         f"  {rep['n_trips']}回合 胜率{rep['win_rate']}% 盈亏比{rep['profit_factor']} | "
         f"盈利单{rep['avg_win_pct']}%/{rep['avg_win_hold']}天 亏损单{rep['avg_loss_pct']}%/{rep['avg_loss_hold']}天"]
    icon = {"alert": "🔴", "warn": "🟡", "info": "🟢"}
    for r in rep["rules"]:
        mark = icon.get(r["severity"], "·") if r["passed"] is not None else "·"
        line = f"  {mark} {r['name']}: {r['detail']}"
        if r["suggestion"]:
            line += f" → {r['suggestion']}"
        L.append(line)
    return "\n".join(L)


if __name__ == "__main__":
    import io
    import os
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_bootstrap.ROOT, ".env"))
    except Exception:
        pass
    print("=== 影子账户·交易行为诊断自检 ===")
    print(format_text(run_diagnose()))
