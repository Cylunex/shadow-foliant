"""组合级回测引擎 — 事件驱动 + 现金账户 + 并发持仓上限

与 backtest_engine.py 的区别(也是它存在的理由)：
  - backtest_engine 是**单股事件统计**：每次触发独立算 N 日收益，无资金约束、
    无并发持仓上限、无组合回撤——多个信号"同时全仓"，系统性高估真实收益。
  - 本引擎按**一个现金账户**逐日撮合：现金有限、最多持 max_positions 只、
    每根 bar 先卖(止损/止盈/到期)释放现金再买、成本拆佣金+印花税+滑点，
    输出组合级 CAGR/最大回撤/夏普/波动/胜率/净值曲线，并对比沪深300。
    这才是"这套策略当年能赚多少、回撤多大"的可信口径。

无前视(no look-ahead)：
  - 信号在 bar t 用截至 t 的数据判定(策略函数 date=t 只回看)。
  - 命中后**次日开盘**建仓(entry = t+1 根 bar 的 open)，绝不用未来数据。
  - 卖出/持仓盯市只用当日及以前的 bar。

接口：
  portfolio_backtest(stocks, start, end, strategy_id='enter', ...) -> dict
  portfolio_backtest_live(stocks, start, end, ...) -> dict   # 用策略基因组 live 集

与 walk-forward 同口径切样本外：传入与 get_live_strategy_set 一致的区间即可。
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导(子目录上 sys.path,支持本模块独立运行)

from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Callable

import numpy as np
import pandas as pd

from backtest_engine import _trigger_dates
from instock_strategy_runner import _normalize_df


# ── 交易成本(A股实盘口径) ──────────────────────────────────────────
#   佣金:双边收，万 2.5，单边最低 5 元
#   印花税:仅卖出收 0.1%
#   滑点:每边按成交价 0.05% 让利(冲击成本近似)
DEFAULT_COMMISSION_PCT = 0.00025   # 万 2.5
DEFAULT_COMMISSION_MIN = 5.0       # 单笔最低佣金(元)
DEFAULT_STAMP_TAX_PCT = 0.001      # 印花税 0.1%(仅卖)
DEFAULT_SLIPPAGE_PCT = 0.0005      # 滑点 0.05%/边
LOT = 100                          # A股一手 = 100 股
TRADING_DAYS = 252


def _buy_fee(amount: float, commission_pct: float) -> float:
    return max(amount * commission_pct, DEFAULT_COMMISSION_MIN)


def _sell_fee(amount: float, commission_pct: float, stamp_tax_pct: float) -> float:
    return max(amount * commission_pct, DEFAULT_COMMISSION_MIN) + amount * stamp_tax_pct


def _load_stock(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """把一只股票的 K 线规整成数组+日期索引(供 O(1) 取价)。"""
    norm = _normalize_df(df)
    if norm is None or len(norm) == 0 or 'date' not in norm.columns:
        return None
    norm = norm.sort_values('date').reset_index(drop=True)
    dates = norm['date'].tolist()
    return {
        'dates': dates,
        'idx': {d: i for i, d in enumerate(dates)},
        'open': norm['open'].to_numpy(dtype='float64'),
        'high': norm['high'].to_numpy(dtype='float64'),
        'low': norm['low'].to_numpy(dtype='float64'),
        'close': norm['close'].to_numpy(dtype='float64'),
        'pchg': norm['p_change'].to_numpy(dtype='float64') if 'p_change' in norm.columns
                else np.zeros(len(dates)),
        '_norm': norm,
    }


def _collect_triggers(sym: str, sd: Dict[str, Any], strategy_id: str,
                      start: str, end: str,
                      params: Optional[Dict[str, Any]]) -> List[Tuple[str, str, float]]:
    """返回该股的 [(entry_date, trigger_date, strength)]。

    无前视:trigger 在 td 判定，entry 取 td 的**下一根 bar**(次日开盘建仓)。
    strength = 触发日涨幅(%)，作"信号强度/把握度"的简单代理(信号加权分配用)。
    """
    tds = _trigger_dates(sd['_norm'], strategy_id, start, end, params=params)
    out = []
    idx, dates = sd['idx'], sd['dates']
    for td in tds:
        i = idx.get(td)
        if i is None or i + 1 >= len(dates):
            continue   # 没有次日 bar → 无法建仓(避免用未来/末尾外推)
        out.append((dates[i + 1], td, float(sd['pchg'][i])))
    return out


def _pbt_worker(task):
    """进程池 worker(必须模块级才可跨进程 pickle)。task=(code,name,combos,start,end,period)。
    自己走 datahub 拉K线(故仅用于默认数据源);返回 (code,name,sd_无_norm,err,[(entry,td,strength)...])。
    回传前剥掉 sd['_norm'](DataFrame),撮合阶段只用 OHLC 数组,免跨进程传大对象。"""
    code, name, combos, start, end, period = task
    import datahub
    try:
        sd = _load_stock(datahub.kline(code, period))
    except Exception as e:
        return (code, name, None, str(e)[:60], [])
    if sd is None:
        return (code, name, None, 'empty', [])
    trigs = []
    for sid, p in combos:
        trigs.extend(_collect_triggers(code, sd, sid, start, end, p))
    sd.pop('_norm', None)
    return (code, name, sd, None, trigs)


def _max_drawdown(equity: np.ndarray) -> float:
    """最大回撤(%，负值)。"""
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak * 100.0
    return float(dd.min())


def _curve_metrics(dates: List[str], equity: np.ndarray,
                   initial_cash: float) -> Dict[str, Any]:
    """从净值曲线算 CAGR/最大回撤/夏普/年化波动/总收益。"""
    if len(equity) < 2:
        return {'total_return_pct': 0.0, 'cagr_pct': 0.0, 'max_dd_pct': 0.0,
                'sharpe': 0.0, 'volatility_pct': 0.0}
    final = float(equity[-1])
    total_ret = (final / initial_cash - 1.0) * 100.0
    d0 = datetime.strptime(dates[0], '%Y-%m-%d')
    d1 = datetime.strptime(dates[-1], '%Y-%m-%d')
    years = max((d1 - d0).days / 365.25, 1e-6)
    cagr = ((final / initial_cash) ** (1.0 / years) - 1.0) * 100.0 if final > 0 else -100.0
    rets = np.diff(equity) / equity[:-1]
    vol = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    sharpe = float(np.mean(rets) / vol * np.sqrt(TRADING_DAYS)) if vol > 0 else 0.0
    return {
        'total_return_pct': round(total_ret, 2),
        'cagr_pct': round(cagr, 2),
        'max_dd_pct': round(_max_drawdown(equity), 2),
        'sharpe': round(sharpe, 2),
        'volatility_pct': round(vol * np.sqrt(TRADING_DAYS) * 100.0, 2),
    }


_BENCH_CACHE: Dict[str, Tuple[float, Dict[str, float]]] = {}   # code -> (ts, date->close);1h TTL


def _benchmark_series(code: str) -> Dict[str, float]:
    """取基准指数 date->close。优先 akshare 指数日线，失败回退 datahub.kline。空则返回 {}。
    进程内 1h 缓存:同一基准被多次回测复用,免每次重抓(单次抓取约 0.9s)。"""
    import time as _t
    ent = _BENCH_CACHE.get(code)
    if ent and _t.time() - ent[0] < 3600:
        return ent[1]
    series = _benchmark_series_fetch(code)
    if series:
        _BENCH_CACHE[code] = (_t.time(), series)
    return series


def _benchmark_series_fetch(code: str) -> Dict[str, float]:
    # 1) 首选 datahub.index_kline(指数专用域:baostock 全历史 + akshare 指数接口,磁盘缓存)。
    #    ⚠️ 别再用 datahub.kline():那是个股链,000300 等指数代码在 6 个个股源上全链必败,
    #    每次基准取数给 kline 域全部源各记一次连败 → 污染健康度/触发 baostock 熔断,
    #    严重时全源熔断黑掉所有个股 K线 120s(2026-07-17 修,详见 datahub.index_kline)。
    try:
        import datahub
        df = datahub.index_kline(code, '3y')
        norm = _normalize_df(df)
        if norm is not None and len(norm):
            return dict(zip(norm['date'], norm['close'].astype('float64')))
    except Exception:
        pass
    # 2) 兜底:akshare 指数日线(沪深300=sh000300)
    try:
        import akshare as ak
        sym = code if code.startswith(('sh', 'sz')) else (
            'sh' + code if code.startswith(('000', '688')) else 'sz' + code)
        df = ak.stock_zh_index_daily(symbol=sym)
        if df is not None and not df.empty:
            d = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
            return dict(zip(d, df['close'].astype('float64')))
    except Exception:
        pass
    return {}


def _benchmark_compare(bench_code: str, dates: List[str],
                       initial_cash: float) -> Optional[Dict[str, Any]]:
    """把基准对齐到回测日历(前向填充缺口)，归一到 initial_cash 算同口径指标。"""
    series = _benchmark_series(bench_code)
    if not series:
        return None
    aligned, last = [], None
    for d in dates:
        v = series.get(d)
        if v is not None:
            last = v
        if last is not None:
            aligned.append(last)
        else:
            aligned.append(np.nan)
    arr = np.array(aligned, dtype='float64')
    mask = ~np.isnan(arr)
    if mask.sum() < 2:
        return None
    base = arr[mask][0]
    equity = arr / base * initial_cash
    # 用对齐后有效段算指标
    valid_dates = [d for d, m in zip(dates, mask) if m]
    m = _curve_metrics(valid_dates, equity[mask], initial_cash)
    m['name'] = bench_code
    m['curve'] = [round(float(x), 2) if not np.isnan(x) else None
                  for x in (arr / base)]   # 净值(归一到 1.0)
    return m


def portfolio_backtest(
    stocks: List[Tuple[str, str]],
    start: str, end: str,
    strategy_id: str = 'enter',
    params: Optional[Dict[str, Any]] = None,
    strategy_combos: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
    hold_days: int = 10,
    stop_pct: Optional[float] = 8.0,
    target_pct: Optional[float] = 15.0,
    max_positions: int = 5,
    initial_cash: float = 1_000_000.0,
    allocation: str = 'equal',              # 'equal' | 'signal'
    commission_pct: float = DEFAULT_COMMISSION_PCT,
    stamp_tax_pct: float = DEFAULT_STAMP_TAX_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    benchmark: Optional[str] = '000300',
    df_fetcher: Optional[Callable[[str, str], pd.DataFrame]] = None,
    period: str = '3y',
    curve_points: int = 0,
    max_workers: int = 8,
) -> Dict[str, Any]:
    """组合级事件驱动回测。

    Args:
        stocks: [(code, name), ...] 候选股票池
        start / end: 'YYYY-MM-DD' 回测区间(信号触发范围;持仓可延伸到 end 之后)
        strategy_id / params: 单策略 + 参数化变体(默认参数 params=None)
        strategy_combos: [(sid, params), ...] 多策略并用;给了它则忽略 strategy_id。
                         任一策略在某股触发即入候选(去重取最早 entry_date / 最大 strength)。
        hold_days: 到期持有天数(按交易日 bar 计)
        stop_pct / target_pct: 相对入场价的止损/止盈%(None=不设)
        max_positions: 并发持仓上限(组合核心约束)
        initial_cash: 初始资金
        allocation: 'equal' 等权(每仓≈总权益/max_positions) | 'signal' 按信号强度加权
        commission_pct / stamp_tax_pct / slippage_pct: 成本拆项
        benchmark: 基准指数代码(默认沪深300);None 不比较
        df_fetcher: fn(code, period)->DataFrame;None 用 datahub.kline
        curve_points: >0 时把净值曲线降采样到约该点数(API 传输用),0=返回全量
        max_workers: 信号计算阶段的进程池并发数(默认数据源+任务量≥24 时启用,实测~3x);
                     =1 强制串行;自定义 df_fetcher 始终串行(闭包不可跨进程)

    Returns:
        {summary, equity_curve, trades, per_stock_triggers, config}
    """
    default_fetcher = df_fetcher is None
    if default_fetcher:
        import datahub
        df_fetcher = lambda c, p: datahub.kline(c, p)

    combos = strategy_combos or [(strategy_id, params)]

    # ── 1) 载入各股 K 线 + 收集触发(entry_date 维度) ──
    # 各股独立,逐日跑策略是全程最重的一段(信号计算 ≈0.2s/股·策略,live 集 ~13 策略时随股池线性膨胀)。
    # 这步是 **CPU 密集**(pandas/numpy):线程被 GIL 卡住反而更慢,故用**进程池**(实测 ~3x);
    # 撮合需单一现金账户,仍串行。仅默认数据源(datahub)能进程化(自定义 df_fetcher 不可跨进程 pickle)。
    data: Dict[str, Dict[str, Any]] = {}
    names: Dict[str, str] = {}
    pending: Dict[str, Dict[str, Tuple[str, float]]] = {}   # entry_date -> {sym: (trigger_date, strength)}
    trig_count = 0
    skipped = []

    def _work(code: str, name: str):
        """串行版 worker(自定义 fetcher / 小任务用)。返回同 _pbt_worker 的五元组。"""
        try:
            sd = _load_stock(df_fetcher(code, period))
        except Exception as e:
            return (code, name, None, str(e)[:60], [])
        if sd is None:
            return (code, name, None, 'empty', [])
        trigs = []
        for sid, p in combos:
            trigs.extend(_collect_triggers(code, sd, sid, start, end, p))
        return (code, name, sd, None, trigs)

    # 进程池仅在"默认数据源 + 任务量够大(摊薄进程启动)"时启用,否则串行(小任务串行更快)
    workload = len(stocks) * max(1, len(combos))
    use_proc = default_fetcher and max_workers and max_workers > 1 and len(stocks) > 1 and workload >= 24
    results = None
    if use_proc:
        try:
            import os as _os2
            from concurrent.futures import ProcessPoolExecutor
            nw = min(max_workers, len(stocks), max(1, (_os2.cpu_count() or 4) - 1))
            tasks = [(c, n, combos, start, end, period) for c, n in stocks]
            with ProcessPoolExecutor(max_workers=nw) as ex:
                results = list(ex.map(_pbt_worker, tasks))
        except Exception:
            results = None   # 进程池不可用(如受限环境)→ 回退串行
    if results is None:
        results = [_work(c, n) for c, n in stocks]

    # 合并按原始股池顺序串行进行 → 结果确定(与并发无关)
    for code, name, sd, err, trigs in results:
        if sd is None:
            skipped.append((code, err))
            continue
        data[code] = sd
        names[code] = name
        for entry_date, td, strength in trigs:
            slot = pending.setdefault(entry_date, {})
            cur = slot.get(code)
            if cur is None or strength > cur[1]:
                slot[code] = (td, strength)
            trig_count += 1

    if not data:
        return {'error': 'no_data', 'skipped': skipped,
                'summary': {'trade_count': 0}, 'equity_curve': [], 'trades': []}

    # ── 2) 主交易日历 = 各股在 [start, ...] 内日期并集(含 end 之后的持仓兑现窗口) ──
    all_dates = set()
    for sd in data.values():
        for d in sd['dates']:
            if d >= start:
                all_dates.add(d)
    calendar = sorted(all_dates)
    if not calendar:
        return {'error': 'empty_calendar', 'summary': {'trade_count': 0},
                'equity_curve': [], 'trades': []}

    # ── 3) 逐日撮合:先卖后买 ──
    cash = float(initial_cash)
    positions: Dict[str, Dict[str, Any]] = {}   # sym -> position
    trades: List[Dict[str, Any]] = []
    equity_dates: List[str] = []
    equity_vals: List[float] = []
    invested_ratios: List[float] = []

    def _price(sym: str, d: str, field: str) -> Optional[float]:
        sd = data[sym]
        i = sd['idx'].get(d)
        return None if i is None else float(sd[field][i])

    def _last_close(sym: str, d: str) -> Optional[float]:
        """d 当日或之前最近一根 close(停牌盯市用)。"""
        sd = data[sym]
        i = sd['idx'].get(d)
        if i is not None:
            return float(sd['close'][i])
        # 二分找 <= d 的最后一根
        ds = sd['dates']
        import bisect
        pos = bisect.bisect_right(ds, d) - 1
        return float(sd['close'][pos]) if pos >= 0 else None

    for d in calendar:
        # —— (a) 先卖:逐持仓查止损/止盈/到期 ——
        for sym in list(positions.keys()):
            pos = positions[sym]
            sd = data[sym]
            i = sd['idx'].get(d)
            if i is None:
                continue   # 当日停牌,不能交易(盯市在 (c) 用最近 close)
            bars_held = i - pos['entry_idx']
            if bars_held <= 0:
                continue   # 建仓当日不在同日卖(entry=open,卖出最早次根)
            low_d, high_d, close_d = float(sd['low'][i]), float(sd['high'][i]), float(sd['close'][i])
            stop, target = pos['stop'], pos['target']
            stop_hit = stop is not None and low_d <= stop
            tp_hit = target is not None and high_d >= target
            reason, raw_exit = None, None
            if stop_hit and tp_hit:
                reason, raw_exit = 'ambiguous', stop     # 同根无法判先后,保守取止损
            elif stop_hit:
                reason, raw_exit = 'stop', stop
            elif tp_hit:
                reason, raw_exit = 'target', target
            elif bars_held >= hold_days:
                reason, raw_exit = 'expiry', close_d
            if reason is None:
                continue
            exit_px = raw_exit * (1 - slippage_pct)       # 卖出滑点让利
            gross = exit_px * pos['shares']
            fee = _sell_fee(gross, commission_pct, stamp_tax_pct)
            cash += gross - fee
            net_ret = (gross - fee - pos['cost_basis']) / pos['cost_basis'] * 100.0
            trades.append({
                'symbol': sym, 'name': names.get(sym, ''),
                'strategy': pos['strategy'],
                'entry_date': pos['entry_date'], 'exit_date': d,
                'entry_price': round(pos['entry_price'], 2),
                'exit_price': round(exit_px, 2),
                'shares': pos['shares'], 'hold_bars': bars_held,
                'ret_pct': round(net_ret, 2), 'exit_reason': reason,
                'pnl': round(gross - fee - pos['cost_basis'], 2),
            })
            del positions[sym]

        # —— (b) 后买:处理当日到期的建仓信号(次日开盘价) ——
        slot = pending.get(d)
        if slot:
            free_slots = max_positions - len(positions)
            if free_slots > 0:
                # 当日盯市权益(用于等权目标仓位)
                holdings_val = sum((_last_close(s, d) or 0) * p['shares']
                                   for s, p in positions.items())
                equity_now = cash + holdings_val
                # 候选:剔除已持仓,按 strength 降序取 free_slots 个
                cands = [(s, info[0], info[1]) for s, info in slot.items()
                         if s not in positions and data[s]['idx'].get(d) is not None]
                cands.sort(key=lambda x: x[2], reverse=True)
                cands = cands[:free_slots]
                if cands:
                    if allocation == 'signal':
                        weights = np.array([max(c[2], 0.1) for c in cands], dtype='float64')
                        weights = weights / weights.sum()
                    else:
                        weights = np.full(len(cands), 1.0 / max_positions)
                    for (sym, td, strength), w in zip(cands, weights):
                        open_d = float(data[sym]['open'][data[sym]['idx'][d]])
                        if open_d <= 0:
                            continue
                        entry_px = open_d * (1 + slippage_pct)       # 买入滑点
                        budget = min(equity_now * w if allocation == 'signal'
                                     else equity_now / max_positions, cash)
                        shares = int(budget // (entry_px * LOT)) * LOT
                        if shares < LOT:
                            continue
                        gross = entry_px * shares
                        fee = _buy_fee(gross, commission_pct)
                        if gross + fee > cash:
                            shares -= LOT
                            if shares < LOT:
                                continue
                            gross = entry_px * shares
                            fee = _buy_fee(gross, commission_pct)
                        cash -= gross + fee
                        positions[sym] = {
                            'entry_date': d, 'entry_idx': data[sym]['idx'][d],
                            'entry_price': entry_px, 'shares': shares,
                            'cost_basis': gross + fee,
                            'stop': entry_px * (1 - stop_pct / 100) if stop_pct else None,
                            'target': entry_px * (1 + target_pct / 100) if target_pct else None,
                            'strategy': pos_strategy(combos, td),
                        }

        # —— (c) 盯市:记录当日权益 ——
        holdings_val = sum((_last_close(s, d) or 0) * p['shares']
                           for s, p in positions.items())
        equity = cash + holdings_val
        equity_dates.append(d)
        equity_vals.append(equity)
        invested_ratios.append(holdings_val / equity if equity > 0 else 0.0)

    # ── 4) 收尾:回测末日按最后 close 强制平仓(未实现→已实现,口径干净) ──
    last_d = calendar[-1]
    for sym in list(positions.keys()):
        pos = positions[sym]
        px = _last_close(sym, last_d)
        if px is None:
            continue
        exit_px = px * (1 - slippage_pct)
        gross = exit_px * pos['shares']
        fee = _sell_fee(gross, commission_pct, stamp_tax_pct)
        cash += gross - fee
        net_ret = (gross - fee - pos['cost_basis']) / pos['cost_basis'] * 100.0
        bars_held = data[sym]['idx'].get(last_d, pos['entry_idx']) - pos['entry_idx']
        trades.append({
            'symbol': sym, 'name': names.get(sym, ''), 'strategy': pos['strategy'],
            'entry_date': pos['entry_date'], 'exit_date': last_d,
            'entry_price': round(pos['entry_price'], 2), 'exit_price': round(exit_px, 2),
            'shares': pos['shares'], 'hold_bars': bars_held,
            'ret_pct': round(net_ret, 2), 'exit_reason': 'final_close',
            'pnl': round(gross - fee - pos['cost_basis'], 2),
        })
        del positions[sym]
    # 末日权益用平仓后的现金更新
    if equity_vals:
        equity_vals[-1] = cash

    # ── 5) 指标 ──
    equity_arr = np.array(equity_vals, dtype='float64')
    summary = _curve_metrics(equity_dates, equity_arr, initial_cash)
    summary['final_equity'] = round(float(equity_arr[-1]), 2)
    summary['initial_cash'] = initial_cash
    summary['avg_exposure_pct'] = round(float(np.mean(invested_ratios)) * 100, 1) if invested_ratios else 0.0

    rets = [t['ret_pct'] for t in trades]
    if rets:
        wins = [r for r in rets if r > 0]
        gains = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        losses = sum(t['pnl'] for t in trades if t['pnl'] < 0)
        summary.update({
            'trade_count': len(trades),
            'win_rate_pct': round(len(wins) / len(trades) * 100, 1),
            'avg_trade_ret_pct': round(float(np.mean(rets)), 2),
            'avg_hold_bars': round(float(np.mean([t['hold_bars'] for t in trades])), 1),
            'profit_factor': round(gains / abs(losses), 2) if losses < 0 else None,
            'best_trade_pct': round(max(rets), 2),
            'worst_trade_pct': round(min(rets), 2),
        })
    else:
        summary['trade_count'] = 0

    # ── 6) 基准对比 ──
    bench = _benchmark_compare(benchmark, equity_dates, initial_cash) if benchmark else None
    if bench:
        summary['benchmark_name'] = bench['name']
        summary['benchmark_return_pct'] = bench['total_return_pct']
        summary['benchmark_cagr_pct'] = bench['cagr_pct']
        summary['benchmark_max_dd_pct'] = bench['max_dd_pct']
        summary['excess_return_pct'] = round(summary['total_return_pct'] - bench['total_return_pct'], 2)

    # ── 7) 净值曲线(可降采样) ──
    nav = (equity_arr / initial_cash)
    curve = list(zip(equity_dates, [round(float(x), 4) for x in nav]))
    if curve_points and len(curve) > curve_points:
        step = len(curve) / curve_points
        idxs = sorted(set([int(i * step) for i in range(curve_points)] + [len(curve) - 1]))
        curve = [curve[i] for i in idxs]
    equity_curve = [{'date': dd, 'nav': nv} for dd, nv in curve]
    if bench and bench.get('curve'):
        bench_nav = bench['curve']
        bmap = dict(zip(equity_dates, bench_nav))
        for pt in equity_curve:
            pt['bench_nav'] = bmap.get(pt['date'])

    return {
        'summary': summary,
        'equity_curve': equity_curve,
        'trades': sorted(trades, key=lambda t: t['exit_date']),
        'config': {
            'strategy': [c[0] for c in combos], 'period': f'{start} ~ {end}',
            'hold_days': hold_days, 'stop_pct': stop_pct, 'target_pct': target_pct,
            'max_positions': max_positions, 'initial_cash': initial_cash,
            'allocation': allocation, 'stocks_count': len(data),
            'trigger_count': trig_count, 'skipped': skipped[:10],
        },
    }


def pos_strategy(combos: List[Tuple[str, Optional[Dict[str, Any]]]], td: str) -> str:
    """单策略时直接返回策略名;多策略并用时返回 'mixed'(精确归因成本高，留作扩展)。"""
    return combos[0][0] if len(combos) == 1 else 'mixed'


def portfolio_backtest_live(stocks: List[Tuple[str, str]], start: str, end: str,
                            **kwargs) -> Dict[str, Any]:
    """用策略基因组 live 集(各策略最优变体 + 达标组合策略)做组合回测。
    与实盘选股 (run_one evolved=True) 同口径。基因组不可用时回退默认 'enter'。"""
    combos: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    try:
        from strategy_genome import get_live_strategy_set
        live = get_live_strategy_set() or {}
        for sid, p in (live.get('base') or {}).items():
            combos.append((sid, p))
        for c in (live.get('composed') or []):
            combos.append(('composed', {'genes': c.get('genes') or []}))
    except Exception:
        pass
    if not combos:
        combos = [('enter', None)]
    kwargs.pop('strategy_id', None)
    kwargs.pop('params', None)
    return portfolio_backtest(stocks, start, end, strategy_combos=combos, **kwargs)


def run_evolution_ab(stocks: List[Tuple[str, str]], start: str, end: str,
                     persist: bool = True, **kwargs) -> Dict[str, Any]:
    """进化效果闭环:进化集 vs 全默认集 的组合级 A/B(同池、同期、同风控)。

    - 进化集: get_live_strategy_set(auto_revert=False) 的真实部署集(各策略最优变体 + 达标 composed);
      强制 auto_revert=False,取真实进化集本身,避免"A/B 喂自动回退、回退又改变 A/B"的自指循环。
    - 默认集: default_strategy_combos()(全 generation=0 InStock 默认参数,无 composed)= 没有进化时的系统。
    返回 {evolved, default, excess, config};persist=True 写入 evolution_ab 表(供日报/UI/自动回退用)。
    """
    from strategy_genome import (get_live_strategy_set, default_strategy_combos,
                                 save_evolution_ab)
    ev_combos: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    try:
        live = get_live_strategy_set(auto_revert=False) or {}
        for sid, p in (live.get('base') or {}).items():
            ev_combos.append((sid, p))
        for c in (live.get('composed') or []):
            ev_combos.append(('composed', {'genes': c.get('genes') or []}))
    except Exception:
        pass
    if not ev_combos:
        ev_combos = [('enter', None)]
    df_combos = default_strategy_combos()

    for k in ('strategy_id', 'params', 'strategy_combos'):
        kwargs.pop(k, None)
    kwargs.setdefault('benchmark', None)   # A/B 不需要基准,省两次额外取数

    ev = portfolio_backtest(stocks, start, end, strategy_combos=ev_combos, **kwargs)
    df = portfolio_backtest(stocks, start, end, strategy_combos=df_combos, **kwargs)
    ev_s = ev.get('summary', {}) or {}
    df_s = df.get('summary', {}) or {}

    def _d(a, b):
        return (round(a - b, 2) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None)
    excess = {
        'return_pct': _d(ev_s.get('total_return_pct'), df_s.get('total_return_pct')),
        'cagr_pct': _d(ev_s.get('cagr_pct'), df_s.get('cagr_pct')),
        'sharpe': _d(ev_s.get('sharpe'), df_s.get('sharpe')),
        'max_dd_pct': _d(ev_s.get('max_dd_pct'), df_s.get('max_dd_pct')),
    }
    if persist:
        try:
            save_evolution_ab(start, end, len(stocks), evolved_n_strat=len(ev_combos),
                              evolved=ev_s, default=df_s)
        except Exception as e:
            print(f'[evolution_ab] 保存失败: {e}')
    return {'evolved': ev_s, 'default': df_s, 'excess': excess,
            'config': {'pool_n': len(stocks), 'period': f'{start} ~ {end}',
                       'evolved_n_strat': len(ev_combos)}}


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    import _bootstrap  # noqa
    print('=== 组合级回测引擎自检 ===')
    import datahub
    pool = [('600519', '茅台'), ('000858', '五粮液'), ('600036', '招商银行'),
            ('000333', '美的集团'), ('600276', '恒瑞医药'), ('002415', '海康威视'),
            ('601318', '中国平安'), ('000651', '格力电器')]
    r = portfolio_backtest(
        pool, start='2024-01-01', end='2025-12-31',
        strategy_id='enter', hold_days=10, stop_pct=8.0, target_pct=15.0,
        max_positions=3, initial_cash=1_000_000, allocation='equal',
        df_fetcher=lambda c, p: datahub.kline(c, p))
    if 'error' in r:
        print('自检失败:', r)
    else:
        s = r['summary']
        print(f"区间 {r['config']['period']}  股票池 {r['config']['stocks_count']} 只  "
              f"触发 {r['config']['trigger_count']} 次")
        print(f"总收益 {s['total_return_pct']}%  CAGR {s['cagr_pct']}%  "
              f"最大回撤 {s['max_dd_pct']}%  夏普 {s['sharpe']}  年化波动 {s['volatility_pct']}%")
        print(f"成交 {s['trade_count']} 笔  胜率 {s.get('win_rate_pct','--')}%  "
              f"平均单笔 {s.get('avg_trade_ret_pct','--')}%  平均持有 {s.get('avg_hold_bars','--')} bar  "
              f"盈亏比 {s.get('profit_factor','--')}  平均仓位 {s['avg_exposure_pct']}%")
        if 'benchmark_return_pct' in s:
            print(f"基准({s['benchmark_name']}) 收益 {s['benchmark_return_pct']}%  "
                  f"超额 {s['excess_return_pct']}%")
        print(f"净值曲线 {len(r['equity_curve'])} 点  末值 {s['final_equity']:.0f}")
