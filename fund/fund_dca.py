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
    dates = pd.DatetimeIndex(sorted(dates))
    if period == 'daily':
        return list(dates)
    picks: List[pd.Timestamp] = []
    if period == 'monthly':
        for _, grp in pd.Series(dates, index=dates).groupby([dates.year, dates.month]):
            cand = grp[grp.dt.day >= day]
            picks.append((cand.iloc[0] if len(cand) else grp.iloc[0]))
    elif period in ('weekly', 'biweekly'):
        iso = dates.isocalendar()
        weekkey = iso.year.astype(int) * 100 + iso.week.astype(int)
        seen = {}
        for d, wk in zip(dates, weekkey):
            seen.setdefault(int(wk), d)
        ordered = [seen[k] for k in sorted(seen)]
        picks = ordered if period == 'weekly' else ordered[::2]
    else:
        raise ValueError(f'未知 period: {period}(可选 {PERIODS})')
    return picks


# ----- 资金加权年化(XIRR,二分法,稳健无依赖) -------------------------
def xirr(cashflows: List[tuple], guess: float = 0.1) -> Optional[float]:
    """cashflows: [(date, amount)],投入为负、回收为正。返回年化内部收益率或 None。"""
    if len(cashflows) < 2:
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
                 fee_rate: float = 0.0015, start: str = None, end: str = None,
                 strategy: str = 'normal', valuation_series=None) -> dict:
    """在历史净值上模拟定投。支持三种策略:

      - normal:    定期定额(每期固定 amount)。
      - valuation: 估值智能定投——每期金额 = amount × 估值倍数(低估多投/高估暂停)。
                   估值分位来源:优先 valuation_series(date→0-100 的分位 Series);
                   缺省用基金自身净值滚动分位作代理(低位=便宜→多投)。
      - value_avg: 价值平均法——令持仓市值按 amount×期数 的目标线增长,每期投
                   max(目标市值−当前市值, 0)(只买不卖版本)。

    Args:
        nav_df: DataFrame[date, unit_nav] 或 (date,nav) 序列
        amount: 每期基准金额(元)
        period: daily/weekly/biweekly/monthly;day: monthly 每月定投日(1-28)
        fee_rate: 申购费率(默认 1.5‰);start/end: 'YYYY-MM-DD' 回测区间

    Returns: dict — 含策略、逐期 trades(带 mult/amount)、权益曲线、年化IRR、最大回撤、对比一次性买入。
    """
    # 规整净值序列
    if isinstance(nav_df, pd.DataFrame):
        col = 'unit_nav' if 'unit_nav' in nav_df.columns else nav_df.columns[-1]
        idxc = 'date' if 'date' in nav_df.columns else None
        s = nav_df.set_index(idxc)[col] if idxc else nav_df.iloc[:, -1]
    else:
        s = pd.Series(nav_df)
    s.index = pd.to_datetime(s.index)
    s = pd.to_numeric(s, errors='coerce').dropna().sort_index()
    if start:
        s = s[s.index >= pd.to_datetime(start)]
    if end:
        s = s[s.index <= pd.to_datetime(end)]
    if len(s) < 2:
        return {'error': '净值数据不足', 'n_points': int(len(s))}

    invest_days = _invest_dates(s.index, period, day)
    if not invest_days:
        return {'error': '无定投执行日'}

    # 估值策略:准备每个净值日的估值分位序列(0-100)
    pct_series = None
    if strategy == 'valuation':
        if valuation_series is not None:
            vs = pd.Series(valuation_series)
            vs.index = pd.to_datetime(vs.index)
            pct_series = vs.reindex(s.index).ffill()
        else:
            from fund_valuation import rolling_percentile_series
            pct_series = rolling_percentile_series(s, window=min(504, max(60, len(s) // 3)))

    cum_shares = 0.0
    total_invested = 0.0
    total_fee = 0.0
    trades = []
    cashflows = []  # XIRR 用
    for k, d in enumerate(invest_days):
        nav = float(s.loc[d])
        if nav <= 0:
            continue
        # —— 按策略决定本期投入金额 ——
        mult = 1.0
        if strategy == 'valuation':
            from fund_valuation import valuation_multiplier
            pv = pct_series.loc[d] if (pct_series is not None and d in pct_series.index) else None
            mult = valuation_multiplier(float(pv)) if pv is not None and not pd.isna(pv) else 1.0
            inv_amt = amount * mult
        elif strategy == 'value_avg':
            target_mv = amount * (k + 1)
            current_mv = cum_shares * nav
            inv_amt = max(target_mv - current_mv, 0.0)
        else:
            inv_amt = amount
        if inv_amt <= 0:
            trades.append({'date': d.strftime('%Y-%m-%d'), 'nav': round(nav, 4),
                           'amount': 0.0, 'fee': 0.0, 'mult': round(mult, 2),
                           'shares': 0.0, 'cum_shares': round(cum_shares, 2),
                           'cost_nav': round(total_invested / cum_shares, 4) if cum_shares else 0.0})
            continue
        fee = inv_amt * fee_rate
        shares = (inv_amt - fee) / nav
        cum_shares += shares
        total_invested += inv_amt
        total_fee += fee
        cost_nav = total_invested / cum_shares if cum_shares else 0.0
        trades.append({
            'date': d.strftime('%Y-%m-%d'), 'nav': round(nav, 4),
            'amount': round(inv_amt, 2), 'fee': round(fee, 2), 'mult': round(mult, 2),
            'shares': round(shares, 2), 'cum_shares': round(cum_shares, 2),
            'cost_nav': round(cost_nav, 4),
        })
        cashflows.append((d, -inv_amt))

    end_date = s.index[-1]
    end_nav = float(s.iloc[-1])
    final_value = cum_shares * end_nav
    profit = final_value - total_invested
    profit_pct = profit / total_invested if total_invested else None
    cashflows.append((end_date, final_value))

    # 权益曲线(每个净值日的市值,只计已买入份额)—— 用于回撤
    shares_by_day = pd.Series(0.0, index=s.index)
    acc = 0.0
    buy_map = {t['date']: t['shares'] for t in trades}
    for d in s.index:
        key = d.strftime('%Y-%m-%d')
        if key in buy_map:
            acc += buy_map[key]
        shares_by_day.loc[d] = acc
    equity = shares_by_day * s
    equity = equity[equity > 0]

    # 对比:期初一次性买入同等总额
    first_nav = float(s.iloc[0])
    lump_shares = (total_invested * (1 - fee_rate)) / first_nav if first_nav else 0.0
    lump_final = lump_shares * end_nav
    lump_profit_pct = (lump_final - total_invested) / total_invested if total_invested else None

    return {
        'strategy': strategy, 'period': period, 'amount': amount, 'fee_rate': fee_rate,
        'start': s.index[0].strftime('%Y-%m-%d'), 'end': end_date.strftime('%Y-%m-%d'),
        'n_invests': sum(1 for t in trades if t['amount'] > 0),
        'total_invested': round(total_invested, 2),
        'total_fee': round(total_fee, 2),
        'cum_shares': round(cum_shares, 2),
        'cost_nav': round(total_invested / cum_shares, 4) if cum_shares else None,
        'end_nav': round(end_nav, 4),
        'final_value': round(final_value, 2),
        'profit': round(profit, 2),
        'profit_pct': round(profit_pct, 4) if profit_pct is not None else None,
        'annualized_irr': round(xirr(cashflows), 4) if xirr(cashflows) is not None else None,
        'max_drawdown': round(_equity_max_drawdown(equity), 4) if _equity_max_drawdown(equity) is not None else None,
        'lump_sum': {
            'final_value': round(lump_final, 2),
            'profit_pct': round(lump_profit_pct, 4) if lump_profit_pct is not None else None,
        },
        'dca_beats_lump': (profit_pct is not None and lump_profit_pct is not None and profit_pct > lump_profit_pct),
        'trades': trades,
        'equity_curve': [{'date': d.strftime('%Y-%m-%d'), 'value': round(float(v), 2)}
                         for d, v in equity.items()],
    }


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
def is_due(plan: dict, today) -> bool:
    """今天是否是该计划的定投执行日。
    monthly: 日==day_of;weekly/biweekly: 周几==day_of(1=周一);daily: 每天。"""
    period = plan.get('period')
    if period == 'daily':
        return True
    if period == 'monthly':
        return today.day == int(plan.get('day_of') or 1)
    if period in ('weekly', 'biweekly'):
        return today.weekday() == ((int(plan.get('day_of') or 1) - 1) % 7)
    return False


def auto_record_due_plans(today=None) -> list:
    """对启用且开了 auto_record 的到期定投计划,按最新确认净值自动落一笔流水(幂等防重)。
    借鉴 portfolio-tracker 的 off_positions 自动加仓思路。返回每条记账结果。
    用确认净值 dwjz(latest_nav)而非盘中估算 gsz,保证记账可复现。"""
    import fund_db
    import fund_data
    if today is None:
        raise ValueError('today 必须显式传入(避免不可复现的 now())')
    fund_db.init_db()
    date_str = today.strftime('%Y-%m-%d')
    done = []
    for p in fund_db.get_plans(only_enabled=True):
        if not p.get('auto_record') or not is_due(p, today):
            continue
        if fund_db.has_transaction_on(p['code'], date_str, 'dca_auto'):
            continue  # 当日已自动记过,跳过(防重复)
        latest = fund_data.latest_nav(p['code'])
        if not latest or not latest.get('unit_nav'):
            continue
        nav = latest['unit_nav']
        fee = float(p['amount']) * 0.0015  # 默认申购费率 1.5‰
        res = fund_db.add_transaction(
            p['code'], '定投', nav=nav, amount=p['amount'], fee=fee,
            trade_date=date_str, name=p.get('name'), source='dca_auto',
            note=f"计划#{p['id']}自动定投", update_position=True)
        done.append(res)
    return done


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
