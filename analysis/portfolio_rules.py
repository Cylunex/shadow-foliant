# -*- coding: utf-8 -*-
"""投资组合体检规则引擎(X-Ray)—— 借鉴 ghostfolio portfolio rules,本地化为 A 股。

思路:把"组合健康"拆成一组**可配置阈值的规则**,逐条评估(达标/超标 + 实测值 + 阈值 + 理由 + 建议),
汇总成体检报告(总分 + 分类 + 统计)。规则纯逻辑,数据只需"持仓 + 一次批量行情",不逐只调接口。

与 shadow-foliant 已有的区别:仓位层(position_sizer)给 AI 提示词用、零散;这里是**规则化、可配置、可观测**的
体检框架,产出结构化报告供 webui 体检页 / 周报 / AI 引用。

用法:
    from portfolio_rules import run_check
    report = run_check()            # 自动拉持仓+行情
    # 或纯函数(可测/可注入):
    from portfolio_rules import evaluate
    report = evaluate(holdings, quotes, config=None)

规则返回(每条):
  {key, name, category, passed(bool|None), value, threshold, severity('info'|'warn'|'alert'),
   detail, suggestion}
报告:
  {rules:[...], score(0-100), passed_n, total_n, by_category:{cat:[rules]}, summary, as_of}
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import _bootstrap  # noqa: F401


# ── 默认阈值(可被 config 覆盖)──
DEFAULTS = {
    "single_max_pct": 20.0,        # 单票市值占比上限
    "top5_max_pct": 60.0,          # 前5大集中度上限
    "hhi_max": 0.18,               # 赫芬达尔指数上限(>0.18 偏集中)
    "min_holdings": 5,             # 持仓只数下限(过少=过度集中)
    "max_holdings": 40,            # 持仓只数上限(过多=难管理)
    "st_max_pct": 5.0,             # ST/退市风险股市值占比上限
    "smallcap_max_pct": 30.0,      # 小市值(<阈值亿)股占比上限
    "smallcap_mcap_yi": 50.0,      # 小市值界定(亿)
    "highpe_max_pct": 30.0,        # 高估值(PE>阈值 或 亏损)股占比上限
    "highpe_threshold": 100.0,     # 高 PE 界定
    "loss_max_pct": 40.0,          # 浮亏股市值占比上限
    "heavy_loss_weight_pct": 10.0, # "重仓"权重界定
    "heavy_loss_pnl_pct": -15.0,   # 重仓深亏界定(浮亏超此)
}

CATEGORY = {
    "concentration": "集中度",
    "quality": "质地",
    "risk": "风险",
    "structure": "结构",
}


def _f(v):
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_context(holdings: List[dict], quotes: Dict[str, dict]) -> dict:
    """持仓 + 行情 → 计算每只权重/市值/浮盈/估值/标签,聚合成上下文。"""
    rows = []
    total_mv = 0.0
    for h in holdings or []:
        code = str(h.get("code") or h.get("symbol") or "").strip()
        if not code:
            continue
        qty = _f(h.get("quantity") or h.get("shares")) or 0
        if qty <= 0:
            continue
        cost = _f(h.get("cost_price") or h.get("cost")) or 0
        q = quotes.get(code) or {}
        price = _f(q.get("price")) or cost or 0
        mv = qty * price
        name = q.get("name") or h.get("name") or code
        pe = _f(q.get("pe_ttm"))
        pb = _f(q.get("pb"))
        mcap_yi = _f(q.get("mcap_yi"))
        pnl_pct = round((price - cost) / cost * 100, 1) if (price > 0 and cost > 0) else None
        is_st = any(t in str(name).upper() for t in ("ST", "*ST", "退"))
        rows.append({"code": code, "name": name, "mv": mv, "price": price, "cost": cost,
                     "pnl_pct": pnl_pct, "pe": pe, "pb": pb, "mcap_yi": mcap_yi, "is_st": is_st})
        total_mv += mv
    for r in rows:
        r["weight"] = (r["mv"] / total_mv * 100) if total_mv > 0 else 0.0
    rows.sort(key=lambda x: x["weight"], reverse=True)
    return {"rows": rows, "total_mv": total_mv, "n": len(rows)}


# ── 规则:每个 fn(ctx, cfg) -> rule dict ──

def _rule(key, name, cat, passed, value, threshold, detail, suggestion="", severity=None):
    if severity is None:
        severity = "info" if passed or passed is None else "warn"
    return {"key": key, "name": name, "category": cat, "passed": passed,
            "value": value, "threshold": threshold, "detail": detail,
            "suggestion": suggestion, "severity": severity}


def _r_single(ctx, cfg):
    rows = ctx["rows"]
    if not rows:
        return _rule("single_max", "单票集中度", "concentration", None, None, cfg["single_max_pct"], "无持仓")
    top = rows[0]
    v = round(top["weight"], 1)
    ok = v <= cfg["single_max_pct"]
    return _rule("single_max", "单票集中度", "concentration", ok, v, cfg["single_max_pct"],
                 f"最大单票 {top['name']} 占 {v}%",
                 "" if ok else f"{top['name']} 仓位偏重,建议减至 {cfg['single_max_pct']:.0f}% 以内",
                 "alert" if v > cfg["single_max_pct"] * 1.5 else ("warn" if not ok else "info"))


def _r_top5(ctx, cfg):
    rows = ctx["rows"]
    v = round(sum(r["weight"] for r in rows[:5]), 1)
    ok = v <= cfg["top5_max_pct"]
    return _rule("top5", "前5大集中度", "concentration", ok, v, cfg["top5_max_pct"],
                 f"前5大持仓合计 {v}%", "" if ok else "前5大过于集中,组合分散度不足")


def _r_hhi(ctx, cfg):
    rows = ctx["rows"]
    hhi = round(sum((r["weight"] / 100) ** 2 for r in rows), 3)
    ok = hhi <= cfg["hhi_max"]
    return _rule("hhi", "分散度(HHI)", "concentration", ok, hhi, cfg["hhi_max"],
                 f"赫芬达尔指数 {hhi}(越低越分散)", "" if ok else "组合集中度偏高,建议增加标的或均衡权重")


def _r_count(ctx, cfg):
    n = ctx["n"]
    if n < cfg["min_holdings"]:
        return _rule("count", "持仓数量", "structure", False, n, cfg["min_holdings"],
                     f"仅 {n} 只,过度集中", "建议增加到 5-15 只以分散个股风险", "warn")
    if n > cfg["max_holdings"]:
        return _rule("count", "持仓数量", "structure", False, n, cfg["max_holdings"],
                     f"{n} 只,过于分散难管理", "标的过多,建议聚焦高确定性品种", "warn")
    return _rule("count", "持仓数量", "structure", True, n, None, f"{n} 只,合理区间")


def _r_st(ctx, cfg):
    rows = ctx["rows"]
    st = [r for r in rows if r["is_st"]]
    v = round(sum(r["weight"] for r in st), 1)
    ok = v <= cfg["st_max_pct"]
    names = "、".join(r["name"] for r in st[:5])
    return _rule("st_risk", "ST/退市风险", "risk", ok, v, cfg["st_max_pct"],
                 f"ST/退市风险股占 {v}%" + (f"({names})" if st else ""),
                 "" if ok else "ST 股退市风险高,建议规避或控制仓位",
                 "alert" if v > cfg["st_max_pct"] else "info")


def _r_smallcap(ctx, cfg):
    rows = ctx["rows"]
    sc = [r for r in rows if r["mcap_yi"] is not None and r["mcap_yi"] < cfg["smallcap_mcap_yi"]]
    v = round(sum(r["weight"] for r in sc), 1)
    ok = v <= cfg["smallcap_max_pct"]
    return _rule("smallcap", "小市值暴露", "risk", ok, v, cfg["smallcap_max_pct"],
                 f"<{cfg['smallcap_mcap_yi']:.0f}亿小市值股占 {v}%",
                 "" if ok else "小市值波动大、流动性弱,注意控制比例")


def _r_highpe(ctx, cfg):
    rows = ctx["rows"]
    hp = [r for r in rows if r["pe"] is not None and (r["pe"] > cfg["highpe_threshold"] or r["pe"] <= 0)]
    v = round(sum(r["weight"] for r in hp), 1)
    ok = v <= cfg["highpe_max_pct"]
    return _rule("highpe", "高估值/亏损", "quality", ok, v, cfg["highpe_max_pct"],
                 f"PE>{cfg['highpe_threshold']:.0f}或亏损股占 {v}%",
                 "" if ok else "高估值/亏损股占比偏高,估值回归风险大")


def _r_loss(ctx, cfg):
    rows = ctx["rows"]
    loss = [r for r in rows if r["pnl_pct"] is not None and r["pnl_pct"] < 0]
    v = round(sum(r["weight"] for r in loss), 1)
    ok = v <= cfg["loss_max_pct"]
    # 重仓深亏单独点名
    heavy = [r for r in rows if r["pnl_pct"] is not None
             and r["weight"] >= cfg["heavy_loss_weight_pct"] and r["pnl_pct"] <= cfg["heavy_loss_pnl_pct"]]
    detail = f"浮亏股市值占 {v}%"
    sug = ""
    sev = "info" if ok else "warn"
    if heavy:
        names = "、".join(f"{r['name']}({r['pnl_pct']}%)" for r in heavy)
        detail += f";重仓深亏:{names}"
        sug = "重仓深亏标的建议检视是否止损或逻辑已变"
        sev = "alert"
    return _rule("loss", "浮亏暴露", "quality", ok and not heavy, v, cfg["loss_max_pct"], detail, sug, sev)


RULES: List[Callable] = [_r_single, _r_top5, _r_hhi, _r_count, _r_st, _r_smallcap, _r_highpe, _r_loss]


def evaluate(holdings: List[dict], quotes: Dict[str, dict], config: Optional[dict] = None) -> dict:
    """纯函数体检:持仓 + 行情 → 体检报告。"""
    cfg = dict(DEFAULTS)
    if config:
        cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
    ctx = _build_context(holdings, quotes)
    rules = []
    for fn in RULES:
        try:
            rules.append(fn(ctx, cfg))
        except Exception as e:
            rules.append(_rule(fn.__name__, fn.__name__, "structure", None, None, None, f"评估异常:{e}"))
    scored = [r for r in rules if r["passed"] is not None]
    passed_n = sum(1 for r in scored if r["passed"])
    score = round(passed_n / len(scored) * 100) if scored else None
    by_cat = {}
    for r in rules:
        by_cat.setdefault(r["category"], []).append(r)
    alerts = [r for r in rules if r["severity"] == "alert"]
    warns = [r for r in rules if r["severity"] == "warn"]
    summary = (f"体检 {score} 分;{passed_n}/{len(scored)} 项达标"
               + (f",{len(alerts)} 项警报" if alerts else "")
               + (f",{len(warns)} 项关注" if warns else ""))
    return {"rules": rules, "score": score, "passed_n": passed_n, "total_n": len(scored),
            "by_category": by_cat, "category_names": CATEGORY,
            "alerts": [r["key"] for r in alerts], "summary": summary,
            "total_mv": round(ctx["total_mv"], 0), "n": ctx["n"]}


def run_check(config: Optional[dict] = None) -> dict:
    """便捷:自动拉持仓(portfolio_db)+ 批量行情(datahub)→ 体检。"""
    try:
        from portfolio_db import portfolio_db
        holdings = [h for h in (portfolio_db.get_all_stocks() or [])
                    if isinstance(h, dict) and float(h.get("quantity") or h.get("shares") or 0) > 0]
    except Exception as e:
        return {"error": f"读持仓失败: {e}", "rules": [], "score": None}
    quotes = {}
    try:
        import datahub
        codes = [str(h.get("code")) for h in holdings if h.get("code")]
        for i in range(0, len(codes), 20):
            quotes.update(datahub.quotes(codes[i:i + 20]) or {})
    except Exception:
        pass
    return evaluate(holdings, quotes, config)


def format_text(report: dict) -> str:
    """体检报告 → 推送/AI 文本。"""
    if report.get("error"):
        return f"组合体检失败:{report['error']}"
    if not report.get("n"):
        return "组合体检:当前无股票持仓"
    L = [f"🩺 组合体检 {report['score']} 分 — {report['summary']}", ""]
    icon = {"alert": "🔴", "warn": "🟡", "info": "🟢"}
    for cat, rules in report["by_category"].items():
        L.append(f"【{report['category_names'].get(cat, cat)}】")
        for r in rules:
            mark = icon.get(r["severity"], "·") if r["passed"] is not None else "·"
            line = f"  {mark} {r['name']}: {r['detail']}"
            if r["suggestion"]:
                line += f" → {r['suggestion']}"
            L.append(line)
        L.append("")
    return "\n".join(L).strip()


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        from dotenv import load_dotenv
        import os
        load_dotenv(os.path.join(_bootstrap.ROOT, ".env"))
    except Exception:
        pass
    print("=== 组合体检自检 ===")
    rep = run_check()
    print(format_text(rep))
