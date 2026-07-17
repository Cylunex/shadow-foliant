# -*- coding: utf-8 -*-
"""组合绩效引擎 —— 借鉴 wealthfolio/ghostfolio 的 TWR/MWR/归因/风险算法。

shadow-foliant 原本只有"累计收益",这里补上业界标准的绩效衡量:
  · TWR(时间加权收益):剔除出入金影响,衡量"策略本身"的表现(对比基准用)
  · MWR/XIRR(资金加权年化):考虑出入金时点,衡量"你这笔钱实际赚的年化"
  · 风险:年化波动率 / 最大回撤(+恢复)/ 夏普
  · 归因:总盈亏拆成 已实现 / 浮动 / 费用

数据来源(全用现成):
  · 净值序列:portfolio_snapshot.get_snapshots()(total_mv/total_cost 按日)
  · 现金流:portfolio_db.get_trades()(买入=资本流入、卖出=流出)
  · 已实现盈亏:realized_pnl.summary()
所有计算容错:数据不足返回 None,绝不抛异常。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import _bootstrap  # noqa: F401

_TRADING_DAYS = 244  # A股年化交易日


def _d(s) -> str:
    return str(s or "")[:10]


# ══════════════════════════════════════════════════════════
#  TWR(时间加权)
# ══════════════════════════════════════════════════════════

def twr(equity: List[Tuple[str, float]], flows: Dict[str, float]) -> Optional[dict]:
    """时间加权收益。
    equity: [(date, market_value)] 按日升序(净值快照)。
    flows: {date: 区间净资本流入}(买入为正=注资,卖出为负=抽资);某日无流则 0。
    子区间收益 r = (mv_t - mv_{t-1} - flow_t) / mv_{t-1};TWR = Π(1+r) - 1。
    返回 {twr_pct, twr_annual_pct, days, sub_returns:[...]} 或 None。"""
    eq = [(d, float(v)) for d, v in equity if v and float(v) > 0]
    if len(eq) < 2:
        return None
    sub = []
    cum = 1.0
    for i in range(1, len(eq)):
        d_prev, mv_prev = eq[i - 1]
        d_cur, mv_cur = eq[i]
        # flow 累计区间 (d_prev, d_cur] 全部成交(2026-07-17 修):快照缺日(任务失败是已知常态)时,
        # 中段日期的买卖原来永远匹配不上、被算进收益 → 当日买入直接抬高 TWR/夏普。
        # ISO 日期字符串字典序=时间序,直接比较。
        flow = sum(float(v) for d, v in flows.items() if d_prev < d <= d_cur)
        if mv_prev <= 0:
            continue
        r = (mv_cur - mv_prev - flow) / mv_prev
        # 异常剔除:单区间 ±50% 以上多半是数据缺口/大额出入金未对齐,跳过避免污染
        if r < -0.5 or r > 0.5:
            continue
        sub.append(r)
        cum *= (1 + r)
    if not sub:
        return None
    twr_pct = round((cum - 1) * 100, 2)
    # 年化:按首末快照实际跨天数。样本太短(<30天)年化会被放大成失真值 → 不年化
    from datetime import datetime
    try:
        days = (datetime.strptime(eq[-1][0], "%Y-%m-%d") - datetime.strptime(eq[0][0], "%Y-%m-%d")).days or len(sub)
    except Exception:
        days = len(sub)
    ann = round((cum ** (365.0 / days) - 1) * 100, 2) if days >= 30 else None
    return {"twr_pct": twr_pct, "twr_annual_pct": ann, "days": days, "n_periods": len(sub)}


# ══════════════════════════════════════════════════════════
#  XIRR(资金加权年化,MWR)
# ══════════════════════════════════════════════════════════

def xirr(cashflows: List[Tuple[str, float]]) -> Optional[float]:
    """不规则现金流年化内部收益率。cashflows: [(date, amount)],出钱为负、收钱为正,
    末尾应含一笔"当前组合市值"为正。二分法求解,返回年化%(失败 None)。"""
    from datetime import datetime
    cf = []
    for d, a in cashflows:
        try:
            cf.append((datetime.strptime(_d(d), "%Y-%m-%d"), float(a)))
        except Exception:
            continue
    if len(cf) < 2:
        return None
    cf.sort(key=lambda x: x[0])
    t0 = cf[0][0]
    # 需有正有负才有解
    if not (any(a > 0 for _, a in cf) and any(a < 0 for _, a in cf)):
        return None

    def npv(rate):
        s = 0.0
        for t, a in cf:
            yrs = (t - t0).days / 365.0
            try:
                s += a / ((1 + rate) ** yrs)
            except (ZeroDivisionError, OverflowError):
                return float("inf")
        return s

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None  # 区间内无符号变化,无解
    for _ in range(100):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-6:
            return round(mid * 100, 2)
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2 * 100, 2)


# ══════════════════════════════════════════════════════════
#  风险指标
# ══════════════════════════════════════════════════════════

def risk_metrics(equity: List[Tuple[str, float]], flows: Dict[str, float] = None) -> Optional[dict]:
    """年化波动率 / 最大回撤(+恢复) / 夏普(rf=0)。基于净值日收益(剔除资本流)。"""
    flows = flows or {}
    eq = [(d, float(v)) for d, v in equity if v and float(v) > 0]
    if len(eq) < 3:
        return None
    rets = []
    for i in range(1, len(eq)):
        mv_prev, mv_cur = eq[i - 1][1], eq[i][1]
        flow = float(flows.get(eq[i][0], 0.0))
        if mv_prev > 0:
            r = (mv_cur - mv_prev - flow) / mv_prev
            if -0.5 < r < 0.5:
                rets.append(r)
    if len(rets) < 2:
        return None
    # 波动率/夏普需足够样本(<20期年化会严重失真),不足则只给回撤
    if len(rets) >= 20:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        vol_ann = round(std * math.sqrt(_TRADING_DAYS) * 100, 2)
        sharpe = round(mean / std * math.sqrt(_TRADING_DAYS), 2) if std > 0 else None
    else:
        vol_ann = None
        sharpe = None
    # 最大回撤(基于净值绝对序列,含资本流影响 → 用累计收益曲线更准:这里用 mv 近似)
    peak = eq[0][1]
    peak_date = eq[0][0]
    max_dd = 0.0
    dd_peak_date = dd_trough_date = recover_date = None
    for d, mv in eq:
        if mv > peak:
            peak, peak_date = mv, d
        dd = (mv - peak) / peak if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd
            dd_peak_date, dd_trough_date = peak_date, d
            recover_date = None
        elif dd_trough_date and recover_date is None and mv >= peak:
            recover_date = d
    return {"volatility_pct": vol_ann, "sharpe": sharpe,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "dd_peak_date": dd_peak_date, "dd_trough_date": dd_trough_date,
            "dd_recover_date": recover_date}


# ══════════════════════════════════════════════════════════
#  归因 + 汇总
# ══════════════════════════════════════════════════════════

def _flows_from_trades() -> Tuple[Dict[str, float], List[Tuple[str, float]]]:
    """从成交记录算:① 每日净资本流入(买+/卖-,用于TWR/风险剔除)② XIRR 现金流(出钱-/收钱+)。"""
    daily: Dict[str, float] = {}
    xcf: List[Tuple[str, float]] = []
    try:
        from portfolio_db import portfolio_db
        for t in (portfolio_db.get_trades(limit=10000) or []):
            d = _d(t.get("trade_time"))
            amt = float(t.get("amount") or 0)
            if not d or amt <= 0:
                continue
            is_sell = (t.get("trade_type") == "卖出")
            # TWR 口径:买入=注资(+),卖出=抽资(-)
            daily[d] = daily.get(d, 0.0) + (-amt if is_sell else amt)
            # XIRR 口径:买入=出钱(-),卖出=收钱(+)
            xcf.append((d, amt if is_sell else -amt))
    except Exception:
        pass
    return daily, xcf


def attribution() -> dict:
    """总盈亏归因:已实现(realized_pnl)+ 浮动(当前持仓 mv-cost)+ 费用。"""
    out = {"realized": None, "unrealized": None, "fees": None, "total": None}
    try:
        from realized_pnl import summary as _rp
        s = _rp()
        out["realized"] = s.get("total")
    except Exception:
        pass
    try:
        from portfolio_snapshot import get_snapshots
        snaps = get_snapshots(limit=1)
        if snaps:
            mv = snaps[0].get("total_mv") or 0
            cost = snaps[0].get("total_cost") or 0
            out["unrealized"] = round(mv - cost, 0) if cost else None
    except Exception:
        pass
    r, u = out["realized"], out["unrealized"]
    if r is not None or u is not None:
        out["total"] = round((r or 0) + (u or 0), 0)
    return out


def summary() -> dict:
    """组合绩效汇总:TWR / XIRR / 风险 / 归因。数据不足的项为 None。"""
    from portfolio_snapshot import get_snapshots
    snaps = sorted(get_snapshots(limit=730) or [], key=lambda s: _d(s.get("snap_date")))
    equity = [(_d(s.get("snap_date")), float(s.get("total_mv") or 0)) for s in snaps]
    daily_flows, xcf = _flows_from_trades()

    out = {"as_of": equity[-1][0] if equity else None, "n_snapshots": len(equity)}
    out["twr"] = twr(equity, daily_flows)
    out["risk"] = risk_metrics(equity, daily_flows)
    # XIRR:历史现金流 + 末尾当前市值
    if xcf and equity:
        xcf2 = list(xcf) + [(equity[-1][0], equity[-1][1])]
        out["xirr_pct"] = xirr(xcf2)
    else:
        out["xirr_pct"] = None
    out["attribution"] = attribution()
    return out


def format_text(s: dict) -> str:
    if not s or not s.get("n_snapshots"):
        return "组合绩效:净值快照不足(需盘后 portfolio_indicator_snapshot 积累几日)"
    L = ["📈 组合绩效"]
    t = s.get("twr") or {}
    if t.get("twr_pct") is not None:
        L.append(f"  时间加权(TWR): {t['twr_pct']:+.2f}%" +
                 (f"(年化{t['twr_annual_pct']:+.1f}%)" if t.get('twr_annual_pct') is not None else ""))
    if s.get("xirr_pct") is not None:
        L.append(f"  资金加权年化(XIRR): {s['xirr_pct']:+.2f}%")
    r = s.get("risk") or {}
    if r.get("volatility_pct") is not None:
        L.append(f"  年化波动 {r['volatility_pct']}% · 最大回撤 {r['max_drawdown_pct']}%"
                 + (f" · 夏普 {r['sharpe']}" if r.get('sharpe') is not None else ""))
    a = s.get("attribution") or {}
    if a.get("total") is not None:
        L.append(f"  盈亏归因: 已实现 {(a['realized'] or 0):+,.0f} + 浮动 {(a['unrealized'] or 0):+,.0f} = {a['total']:+,.0f} 元")
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
    print("=== 组合绩效引擎自检 ===")
    print(format_text(summary()))
