"""定投引擎 —— 阶段一:普通定投(定期定额)+ 移动加权成本 + 定投回测(对比一次性买入)。

核心:
  dca_backtest(nav_df, amount, period, ...) 在历史净值上模拟定期定额申购,
  输出累计份额/投入/市值/收益/年化(资金加权 XIRR)/最大回撤,并与「期初一次性买入」对比。

后续阶段二再加:估值智能定投(低估加倍/高估暂停)、价值平均法、目标止盈。
这些都在本文件以 strategy 形式扩展,接口保持 dca_backtest 为统一入口。
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

PERIODS = ('daily', 'weekly', 'biweekly', 'monthly')


# ----- 移动加权成本(给真实定投记账复用) -------------------------------
def moving_cost(prev_shares: float, prev_cost: float,
                buy_shares: float, buy_nav: float) -> float:
    """加仓后的移动加权成本净值。prev_cost 为加权成本(净值口径)。"""
    total_sh = (prev_shares or 0) + (buy_shares or 0)
    if total_sh <= 0:
        return 0.0
    prev_cost = prev_cost or 0.0
    return ((prev_shares or 0) * prev_cost + (buy_shares or 0) * buy_nav) / total_sh


# ----- 定投日期选择 -----------------------------------------------------
def _invest_dates(dates: pd.DatetimeIndex, period: str, day: int = 1) -> List[pd.Timestamp]:
    """从交易(净值)日期序列里挑出定投执行日。
    monthly: 每月第一个 day(日)≥指定日的交易日;weekly/biweekly: 每(两)周首个交易日;daily: 每日。"""
    from fund.fund_calendar import executions
    dates = pd.DatetimeIndex(sorted(dates))
    if not len(dates): return []
    rows = executions(dates[0].date(),dates[-1].date(),[d.date() for d in dates],period,day,
                      dates[0].date() if period=='biweekly' else None)
    return [pd.Timestamp(r['execution_date']) for r in rows]



# ----- 资金加权年化(XIRR,二分法,稳健无依赖) -------------------------
def xirr(cashflows: List[tuple], guess: float = 0.1) -> Optional[float]:
    """cashflows: [(date, amount)],投入为负、回收为正。返回年化内部收益率或 None。"""
    # Aggregate same-day flows and reject ambiguous multiple-sign-change roots.
    grouped={}
    for dt,value in cashflows: grouped[dt]=grouped.get(dt,0)+value
    cashflows=sorted((dt,value) for dt,value in grouped.items() if abs(value)>1e-10)
    signs=[1 if value>0 else -1 for _,value in cashflows]
    if sum(a!=b for a,b in zip(signs,signs[1:]))>1: return None
    if len(cashflows) < 2 or (cashflows[-1][0]-cashflows[0][0]).days<30:
        return None
    cfs = sorted(cashflows, key=lambda x: x[0])
    t0 = cfs[0][0]
    years = [((d - t0).days) / 365.0 for d, _ in cfs]
    amts = [a for _, a in cfs]
    if not (any(a < 0 for a in amts) and any(a > 0 for a in amts)):
        return None

    def npv(r):
        return sum(a / ((1 + r) ** t) for a, t in zip(amts, years))

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None  # 区间内无符号变化,二分不适用
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return float(mid)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return float((lo + hi) / 2)


def _equity_max_drawdown(equity: pd.Series) -> Optional[float]:
    if len(equity) < 2:
        return None
    roll = equity.cummax()
    dd = (equity - roll) / roll.replace(0, np.nan)
    return float(-dd.min()) if dd.notna().any() else None


# ----- 定投回测(核心入口) ---------------------------------------------
def dca_backtest(nav_df, amount: float, period: str = 'monthly', day: int = 1,
                 fee_rate: float = .0015, start=None, end=None, strategy='normal',
                 valuation_series=None, **options):
    from fund.fund_backtest import simulate
    if not isinstance(nav_df,pd.DataFrame):
        raise ValueError('需要带真实日期的净值 DataFrame')
    return simulate(nav_df,amount,period,day,fee_rate,start,end,strategy,valuation_series,**options)


def evaluate_take_profit(cost_nav: float, latest_nav: float, target_pct: float,
                         drawdown_from_peak: float = None, giveback_pct: float = None) -> dict:
    """止盈评估。规则:① 浮盈≥目标 → 建议止盈;② 若给了峰值回撤 giveback,
    达目标后又从峰值回吐≥giveback → 强烈建议止盈(回撤止盈)。
    返回 {profit_pct, hit_target, suggest, reason}。"""
    if not cost_nav or cost_nav <= 0 or not latest_nav:
        return {'profit_pct': None, 'hit_target': False, 'suggest': False, 'reason': '数据不足'}
    pnl = (latest_nav - cost_nav) / cost_nav
    hit = target_pct is not None and pnl >= float(target_pct)
    suggest, reason = hit, ''
    if hit:
        reason = f'浮盈 {pnl:+.1%} ≥ 目标 {float(target_pct):+.0%}'
        if giveback_pct and drawdown_from_peak is not None and drawdown_from_peak >= giveback_pct:
            reason += f';且自峰值回吐 {drawdown_from_peak:.1%} ≥ {giveback_pct:.0%} → 建议落袋'
    return {'profit_pct': round(pnl, 4), 'hit_target': hit, 'suggest': suggest, 'reason': reason}


# ----- 定投计划:到期判定 + 自动记账(阶段二) ------------------------
def is_due(plan: dict, today, open_dates=None) -> bool:
    from fund.fund_calendar import executions
    from datetime import timedelta
    end = today.date() if hasattr(today,'hour') else today
    anchor = plan.get('anchor_date')
    if plan.get('period') == 'biweekly' and not anchor: return False
    start = max(end-timedelta(days=35), __import__('datetime').date.fromisoformat(str(anchor)[:10])) if anchor else end-timedelta(days=35)
    if open_dates is None:
        # Runtime must use an authoritative product calendar, not weekday guesses.
        from fund.fund_service import open_dates_for
        open_dates = open_dates_for(plan,start,end)
    return any(r['execution_date']==str(end) for r in executions(start,end,open_dates,
               plan.get('period','monthly'),plan.get('day_of') or 1,anchor))


def auto_record_due_plans(today=None):
    """Compatibility name: create pending occurrences, never confirmed holdings."""
    if today is None: raise ValueError('today 必须显式传入')
    from fund.fund_service import process_due_plans
    return process_due_plans(today)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    idx = pd.date_range('2021-01-01', periods=600, freq='D')
    rng = np.random.default_rng(7)
    nav = pd.DataFrame({'date': idx, 'unit_nav': (1 + rng.normal(0.0003, 0.013, 600)).cumprod()})
    r = dca_backtest(nav, 1000, 'monthly', day=5)
    for k in ('n_invests', 'total_invested', 'final_value', 'profit_pct', 'annualized_irr',
              'max_drawdown', 'dca_beats_lump'):
        print(k, '=', r[k])
    print('lump_sum:', r['lump_sum'])
