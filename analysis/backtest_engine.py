"""批量回测引擎 — 配合 instock_strategy_runner

设计：
  - 单股回测：在历史区间内逐日跑某策略，记录每次触发后 N 日收益曲线
  - 批量汇总：胜率 / 平均收益 / 最大单次收益/亏损 / 最大回撤
  - 注意：无手续费/滑点/资金管理建模，仅用于"策略有效性筛选"，不用于实盘评估

接口：
  backtest_one(symbol, strategy_id, df, start, end, hold_days=10)
  backtest_batch(stocks, strategy_id, start, end, hold_days=10)

可入库：results 字段是 list[dict]，可序列化存 PG 的 backtest_results 表
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd

from selection.instock_strategy_runner import STRATEGIES, _normalize_df


def _trigger_dates(df: pd.DataFrame, strategy_id: str,
                   start: str, end: str,
                   params: Optional[Dict[str, Any]] = None) -> List[str]:
    """在 [start, end] 内逐日跑策略，返回所有触发日期列表
    
    params: 可选参数覆盖（例如 {'threshold': 45}），
            支持策略基因组的参数化变体。
    """
    # 组合策略(strategy_composer 产出的全新策略):基因在 params['genes']
    if strategy_id == 'composed':
        genes = (params or {}).get('genes') or []
        if not genes:
            return []
        from analysis.strategy_composer import check_composed
        def func(code_name_arg, data_arg, date=None):
            return check_composed(code_name_arg, data_arg, date=date, genes=genes)
    else:
        meta = STRATEGIES.get(strategy_id)
        if meta is None:
            return []
        func = meta['func']
        # 若有参数化变体，创建 wrapper 覆盖默认值。
        # 2026-06-12 修复:原来只透传 threshold,基因组其他参数(量比/涨幅/MA周期等)全被忽略
        # → 变异体回测结果与父代相同,进化是空转。现按策略函数签名透传全部匹配参数。
        if params:
            import inspect
            base_func = func
            try:
                accepted = set(inspect.signature(base_func).parameters)
            except (TypeError, ValueError):
                accepted = {'threshold'}
            kw_all = {k: v for k, v in params.items() if k in accepted}
            def _wrapped(code_name_arg, data_arg, date=None):
                return base_func(code_name_arg, data_arg, date=date, **kw_all)
            func = _wrapped
    
    triggers: List[str] = []
    # 默认使用全数据范围
    if not start:
        start = df['date'].min()
    if not end:
        end = df['date'].max()
    dates = df.loc[(df['date'] >= start) & (df['date'] <= end), 'date'].tolist()
    for d in dates:
        try:
            ok = func(('', ''), df, date=datetime.strptime(d, '%Y-%m-%d'))
            if ok:
                triggers.append(d)
        except Exception:
            continue
    return triggers


# 交易成本(借鉴 execution-model skill):双边往返成本占比(%)
#   佣金双边≈0.05% + 印花税0.1%(卖) + 滑点≈0.05%*2 ≈ 0.25%,取保守 0.2%
DEFAULT_COST_PCT = 0.2


def _forward_return(df: pd.DataFrame, trigger_date: str, hold_days: int,
                    cost_pct: float = DEFAULT_COST_PCT,
                    stop_pct: Optional[float] = None,
                    target_pct: Optional[float] = None) -> Optional[Dict[str, float]]:
    """从触发日次日起，持有 hold_days 天的收益统计(借鉴 daily_stock_analysis 双收益)。

    返回: {entry_price, exit_price, max_high, min_low, ret_pct(毛), ret_pct_net(扣成本), max_dd_pct,
           [若给了 stop_pct/target_pct] ret_pct_disciplined(纪律收益), first_hit, first_hit_day}
    - ret_pct: 持有到期(窗口末 close)的实际收益(无纪律)。
    - ret_pct_disciplined: 逐根K线检测先触止损还是止盈 → 在触发处了结的收益;同根都触发记 ambiguous(保守取止损)。
      未给 stop_pct/target_pct 时等于 ret_pct。
    stop_pct/target_pct 为相对入场价的百分比(如 stop_pct=8 表示 -8% 止损,target_pct=15 表示 +15% 止盈)。
    """
    idx = df.index[df['date'] == trigger_date]
    if len(idx) == 0:
        return None
    i = idx[0]
    if i + 1 + hold_days > len(df):
        return None
    window = df.iloc[i + 1: i + 1 + hold_days]
    if len(window) == 0:
        return None
    entry = float(window.iloc[0]['open'])
    exit_p = float(window.iloc[-1]['close'])
    max_h = float(window['high'].max())
    min_l = float(window['low'].min())
    ret = (exit_p - entry) / entry * 100
    cum_max = np.maximum.accumulate(window['close'].values)
    drawdown = (window['close'].values - cum_max) / cum_max * 100
    max_dd = float(drawdown.min())
    out = {
        'entry_price': round(entry, 2),
        'exit_price': round(exit_p, 2),
        'max_high': round(max_h, 2),
        'min_low': round(min_l, 2),
        'ret_pct': round(ret, 2),
        'ret_pct_net': round(ret - cost_pct, 2),
        'max_dd_pct': round(max_dd, 2),
    }
    # —— 双收益:带止损/止盈纪律的逐K线模拟退出 ——
    sl = entry * (1 - stop_pct / 100) if stop_pct else None
    tp = entry * (1 + target_pct / 100) if target_pct else None
    if sl or tp:
        first_hit, sim_exit, hit_day = 'hold', exit_p, len(window)
        for d, (_, bar) in enumerate(window.iterrows(), start=1):
            stop_hit = sl is not None and float(bar['low']) <= sl
            tp_hit = tp is not None and float(bar['high']) >= tp
            if stop_hit and tp_hit:
                first_hit, sim_exit, hit_day = 'ambiguous', sl, d  # 同根K线无法判先后,保守取止损
                break
            if stop_hit:
                first_hit, sim_exit, hit_day = 'stop', sl, d
                break
            if tp_hit:
                first_hit, sim_exit, hit_day = 'target', tp, d
                break
        out['ret_pct_disciplined'] = round((sim_exit - entry) / entry * 100 - cost_pct, 2)
        out['first_hit'] = first_hit
        out['first_hit_day'] = hit_day
    else:
        out['ret_pct_disciplined'] = out['ret_pct_net']
        out['first_hit'] = 'hold'
        out['first_hit_day'] = len(window)
    return out


def _discipline_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """纪律(止损止盈)统计:双收益对比 + 触发率 + 平均触发天数。trades 需含 ret_pct_disciplined/first_hit。"""
    n = len(trades)
    if n == 0:
        return {}
    disc = [t.get('ret_pct_disciplined', t.get('ret_pct_net', t['ret_pct'])) for t in trades]
    actual_net = [t.get('ret_pct_net', t['ret_pct']) for t in trades]
    hits = [t.get('first_hit', 'hold') for t in trades]
    stop_n = sum(1 for h in hits if h in ('stop', 'ambiguous'))
    tgt_n = sum(1 for h in hits if h == 'target')
    amb_n = sum(1 for h in hits if h == 'ambiguous')
    resolved = [t.get('first_hit_day') for t in trades if t.get('first_hit') in ('stop', 'target', 'ambiguous')]
    return {
        'avg_ret_disciplined_pct': round(float(np.mean(disc)), 2),
        'discipline_impact_pct': round(float(np.mean(disc)) - float(np.mean(actual_net)), 2),  # 纪律 vs 持有到期 的收益差
        'stop_trigger_rate': round(stop_n / n * 100, 1),
        'target_trigger_rate': round(tgt_n / n * 100, 1),
        'ambiguous_rate': round(amb_n / n * 100, 1),
        'avg_days_to_hit': round(float(np.mean(resolved)), 1) if resolved else None,
    }


def backtest_one(symbol: str, strategy_id: str, df: pd.DataFrame,
                 start: str, end: str, hold_days: int = 10,
                 name: str = '', stop_pct: Optional[float] = None,
                 target_pct: Optional[float] = None,
                 params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """单股回测某策略

    Args:
        symbol: 股票代码
        strategy_id: STRATEGIES 中的 id
        df: K 线 DataFrame
        start / end: 'YYYY-MM-DD'，回测区间（触发日期范围）
        hold_days: 触发后持有天数
        stop_pct / target_pct: 可选,给了则额外算"纪律收益"(逐K止损止盈)+触发率(评估止损止盈位是否合理)
    """
    norm = _normalize_df(df)
    if len(norm) == 0:
        return {'symbol': symbol, 'strategy': strategy_id, 'error': 'empty_dataframe',
                'triggers': [], 'summary': {}}

    triggers = _trigger_dates(norm, strategy_id, start, end, params=params)
    trades = []
    for td in triggers:
        r = _forward_return(norm, td, hold_days, stop_pct=stop_pct, target_pct=target_pct)
        if r is not None:
            r['trigger_date'] = td
            trades.append(r)

    if not trades:
        return {
            'symbol': symbol, 'name': name, 'strategy': strategy_id,
            'triggers': triggers, 'trades': [],
            'summary': {'count': 0},
        }

    rets = [t['ret_pct'] for t in trades]
    rets_net = [t.get('ret_pct_net', t['ret_pct']) for t in trades]
    win = [r for r in rets if r > 0]
    win_net = [r for r in rets_net if r > 0]
    summary = {
        'count': len(trades),
        'win_rate': round(len(win) / len(trades) * 100, 1),
        'win_rate_net': round(len(win_net) / len(trades) * 100, 1),
        'avg_ret_pct': round(float(np.mean(rets)), 2),
        'avg_ret_net_pct': round(float(np.mean(rets_net)), 2),
        'median_ret_pct': round(float(np.median(rets)), 2),
        'max_win_pct': round(max(rets), 2),
        'max_loss_pct': round(min(rets), 2),
        'avg_max_dd_pct': round(float(np.mean([t['max_dd_pct'] for t in trades])), 2),
    }
    summary.update(_discipline_stats(trades))
    return {
        'symbol': symbol, 'name': name, 'strategy': strategy_id,
        'hold_days': hold_days, 'period': f'{start} ~ {end}',
        'triggers': triggers, 'trades': trades, 'summary': summary,
    }


def backtest_batch(stocks: List[Tuple[str, str]], strategy_id: str,
                   start: str, end: str, hold_days: int = 10,
                   df_fetcher=None, period: str = '3y',
                   stop_pct: Optional[float] = None,
                   target_pct: Optional[float] = None,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """批量回测某策略：所有股票合并出胜率/平均收益。params=参数化变体(同 backtest_one)"""
    if df_fetcher is None:
        from stock_data import StockDataFetcher
        fetcher = StockDataFetcher()
        df_fetcher = lambda s, p: fetcher.get_stock_data(s, p)

    all_trades = []
    per_stock = []
    for symbol, name in stocks:
        try:
            df = df_fetcher(symbol, period)
            r = backtest_one(symbol, strategy_id, df, start, end, hold_days, name=name,
                             stop_pct=stop_pct, target_pct=target_pct, params=params)
            per_stock.append(r)
            all_trades.extend(r.get('trades', []))
        except Exception as e:
            per_stock.append({'symbol': symbol, 'name': name, 'error': str(e)})

    if not all_trades:
        return {'strategy': strategy_id, 'stocks_count': len(stocks),
                'summary': {'count': 0}, 'per_stock': per_stock}

    rets = [t['ret_pct'] for t in all_trades]
    rets_net = [t.get('ret_pct_net', t['ret_pct']) for t in all_trades]
    win = [r for r in rets if r > 0]
    win_net = [r for r in rets_net if r > 0]
    summary = {
        'count': len(all_trades),
        'win_rate': round(len(win) / len(all_trades) * 100, 1),
        'win_rate_net': round(len(win_net) / len(all_trades) * 100, 1),
        'avg_ret_pct': round(float(np.mean(rets)), 2),
        'avg_ret_net_pct': round(float(np.mean(rets_net)), 2),
        'median_ret_pct': round(float(np.median(rets)), 2),
        'max_win_pct': round(max(rets), 2),
        'max_loss_pct': round(min(rets), 2),
        'avg_max_dd_pct': round(float(np.mean([t['max_dd_pct'] for t in all_trades])), 2),
    }
    summary.update(_discipline_stats(all_trades))
    return {
        'strategy': strategy_id, 'strategy_cn': STRATEGIES.get(strategy_id, {}).get('cn', '?'),
        'hold_days': hold_days, 'period': f'{start} ~ {end}',
        'stocks_count': len(stocks),
        'summary': summary, 'per_stock': per_stock,
    }


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 回测引擎自检 ===')
    from stock_data import StockDataFetcher
    f = StockDataFetcher()
    df = f.get_stock_data('600519', '2y')
    for sid in ['enter', 'parking_apron', 'keep_increasing']:
        r = backtest_one('600519', sid, df,
                         start='2025-01-01', end='2026-05-01', hold_days=10,
                         name='茅台')
        s = r['summary']
        print(f"\n[{sid}] 茅台 2025-01 ~ 2026-05 (持有10日)")
        if s.get('count', 0) == 0:
            print('  零触发')
            continue
        print(f"  触发次数: {s['count']}  胜率: {s['win_rate']}%  "
              f"平均收益: {s['avg_ret_pct']}%  中位数: {s['median_ret_pct']}%")
        print(f"  最大单次盈: {s['max_win_pct']}%  最大单次亏: {s['max_loss_pct']}%  "
              f"平均最大回撤: {s['avg_max_dd_pct']}%")
