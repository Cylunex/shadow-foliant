"""InStock 13 套策略统一运行器

迁移自 myhhub/stock 项目（instock/core/strategy/），保持原 `check(...)` 接口不变。
本模块负责：
  1. 适配 shadow-foliant 的 DataFrame（yfinance 大写列 / 中文列 → InStock 小写 + p_change）
  2. 提供单股 / 批量执行入口
  3. 暴露给 AI 选股 / 智策板块

13 套策略一览（按理念分类）：
  📈 短线突破：parking_apron(停机坪) / high_tight_flag(高而窄旗形) / breakthrough_platform(突破平台)
  📊 趋势跟踪：turtle_trade(海龟) / keep_increasing(均线多头) / backtrace_ma250(回踩年线)
  ⚖️ 量价信号：enter(放量上涨) / climax_limitdown(放量跌停)
  🛡️ 稳健：low_backtrace_increase(无大幅回撤) / low_atr(低 ATR 成长)
  🧬 自进化：rsi_oversold_bounce(RSI超卖反弹) / bollinger_squeeze_breakout(布林收窄突破) / weekly_trend_daily_signal(周线趋势+日线信号)
"""

import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

from instock_strategies import (
    backtrace_ma250,
    breakthrough_platform,
    climax_limitdown,
    enter,
    high_tight_flag,
    keep_increasing,
    low_atr,
    low_backtrace_increase,
    parking_apron,
    turtle_trade,
    rsi_oversold_bounce,
    bollinger_squeeze_breakout,
    weekly_trend_daily_signal,
)


STRATEGIES: Dict[str, Dict[str, Any]] = {
    'parking_apron':           {'cn': '停机坪',       'func': parking_apron.check,           'category': '短线突破', 'min_days': 15},
    'high_tight_flag':         {'cn': '高而窄旗形',   'func': high_tight_flag.check_high_tight, 'category': '短线突破', 'min_days': 60},
    'breakthrough_platform':   {'cn': '突破平台',     'func': breakthrough_platform.check,   'category': '短线突破', 'min_days': 60},
    'turtle_trade':            {'cn': '海龟交易',     'func': turtle_trade.check_enter,      'category': '趋势跟踪', 'min_days': 60},
    'keep_increasing':         {'cn': '均线多头',     'func': keep_increasing.check,         'category': '趋势跟踪', 'min_days': 30},
    'backtrace_ma250':         {'cn': '回踩年线',     'func': backtrace_ma250.check,         'category': '趋势跟踪', 'min_days': 250},
    'enter':                   {'cn': '放量上涨',     'func': enter.check_volume,            'category': '量价信号', 'min_days': 61},
    'climax_limitdown':        {'cn': '放量跌停',     'func': climax_limitdown.check,        'category': '量价信号', 'min_days': 60},
    'low_backtrace_increase':  {'cn': '无大幅回撤',   'func': low_backtrace_increase.check,  'category': '稳健成长', 'min_days': 60},
    'low_atr':                 {'cn': '低ATR成长',    'func': low_atr.check_low_increase,    'category': '稳健成长', 'min_days': 250},
    'rsi_oversold_bounce':     {'cn': 'RSI超卖反弹',  'func': rsi_oversold_bounce.check,     'category': '反转捕捉', 'min_days': 60},
    'bollinger_squeeze_breakout': {'cn': '布林收窄突破', 'func': bollinger_squeeze_breakout.check, 'category': '压缩爆发', 'min_days': 100},
    'weekly_trend_daily_signal': {'cn': '周线趋势+日线', 'func': weekly_trend_daily_signal.check, 'category': '多周期共振', 'min_days': 120},
}


_COLUMN_MAP = {
    'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume',
    'Date': 'date',
    '日期': 'date', '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low',
    '成交量': 'volume', '涨跌幅': 'p_change',
}


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """适配 yfinance / akshare / 中文 列名 + 补 date / p_change"""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.copy()
    df = df.rename(columns={k: v for k, v in _COLUMN_MAP.items() if k in df.columns})
    if 'date' not in df.columns:
        if df.index.name in ('Date', 'date', '日期'):
            df = df.reset_index().rename(columns={df.index.name: 'date'})
        elif isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index().rename(columns={'index': 'date'})
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col not in df.columns:
            return pd.DataFrame()
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')
    if 'p_change' not in df.columns:
        df['p_change'] = df['close'].pct_change() * 100
    df['p_change'] = df['p_change'].fillna(0).astype('float64')
    return df.reset_index(drop=True)


# ── 策略基因组 live 集缓存(进化后的最优参数 + 达标组合策略),1小时刷新,失败回退默认 ──
_GENOME_LIVE = {'ts': 0.0, 'live': None}


def _live_genome_set() -> Dict[str, Any]:
    import time as _time
    if _GENOME_LIVE['live'] is not None and _time.time() - _GENOME_LIVE['ts'] < 3600:
        return _GENOME_LIVE['live']
    live = {'base': {}, 'composed': []}
    try:
        from analysis.strategy_genome import get_live_strategy_set
        live = get_live_strategy_set() or live
    except Exception:
        pass
    _GENOME_LIVE['live'] = live
    _GENOME_LIVE['ts'] = _time.time()
    return live


def run_one(symbol: str, df: pd.DataFrame, name: str = '',
            date: Optional[datetime] = None,
            strategies: Optional[List[str]] = None,
            evolved: bool = False) -> Dict[str, Any]:
    """对单只股票跑全部 13 套（或指定子集）策略

    Args:
        symbol: 股票代码（仅用于结果标识，不影响判断）
        df: K 线 DataFrame（任何主流格式）
        name: 股票名称（标识用）
        date: 截止日期，None=最新
        strategies: 指定策略 id 子集，None=全部 13 套
        evolved: True 时用策略基因组进化出的最优参数跑 13 套,并附加达标的组合新策略
                 (基因组不可用时自动回退默认参数,行为不变)

    Returns:
        {symbol, name, total_strategies, matched: [{id, cn, category}], errors}
    """
    norm = _normalize_df(df)
    code_name = (symbol, name)  # InStock 内部用 code_name[0] 取最新日期，签名兼容

    if len(norm) == 0:
        return {'symbol': symbol, 'name': name, 'total_strategies': 0,
                'matched': [], 'errors': ['empty_dataframe']}

    code_name = (norm['date'].iloc[-1] if 'date' in norm.columns else None, name)

    live = _live_genome_set() if evolved else {'base': {}, 'composed': []}

    target = list(strategies) if strategies else list(STRATEGIES.keys())
    matched: List[Dict[str, str]] = []
    errors: List[str] = []
    for sid in target:
        meta = STRATEGIES.get(sid)
        if meta is None:
            errors.append(f'unknown_strategy:{sid}')
            continue
        if len(norm) < meta['min_days']:
            continue
        try:
            kw = {}
            best = live['base'].get(sid)
            if best:
                # 按函数签名过滤(进化参数 → 策略 kwarg)
                import inspect
                accepted = set(inspect.signature(meta['func']).parameters)
                kw = {k: v for k, v in best.items() if k in accepted}
            ok = meta['func'](code_name, norm, date=date, **kw)
            if ok:
                matched.append({'id': sid, 'cn': meta['cn'], 'category': meta['category']})
        except Exception as e:
            errors.append(f"{sid}: {e}")

    # 组合新策略(基因组进化产出,score 达标才会进 live 集)
    n_composed = 0
    if live['composed']:
        try:
            from analysis.strategy_composer import check_composed
            for c in live['composed']:
                n_composed += 1
                try:
                    if check_composed(code_name, norm, date=date, genes=c.get('genes') or []):
                        matched.append({'id': f"composed:{c['vid']}",
                                        'cn': c.get('cn') or '组合策略',
                                        'category': '🧪进化新策略'})
                except Exception:
                    continue
        except Exception:
            pass

    return {
        'symbol': symbol, 'name': name,
        'total_strategies': len(target) + n_composed,
        'matched': matched,
        'matched_count': len(matched),
        'errors': errors,
    }


def run_batch(stocks: List[Tuple[str, str]],
              df_fetcher=None,
              date: Optional[datetime] = None,
              strategies: Optional[List[str]] = None,
              period: str = '2y',
              evolved: bool = False) -> List[Dict[str, Any]]:
    """对多只股票批量跑策略

    Args:
        stocks: [(symbol, name), ...]
        df_fetcher: 自定义 K 线获取函数 fn(symbol, period) -> DataFrame；
                    None 时用 stock_data.StockDataFetcher
        date / strategies / period / evolved: 同 run_one
    """
    if df_fetcher is None:
        from stock_data import StockDataFetcher
        fetcher = StockDataFetcher()
        df_fetcher = lambda s, p: fetcher.get_stock_data(s, p)

    results = []
    for symbol, name in stocks:
        try:
            df = df_fetcher(symbol, period)
            r = run_one(symbol, df, name=name, date=date, strategies=strategies, evolved=evolved)
            results.append(r)
        except Exception as e:
            results.append({'symbol': symbol, 'name': name, 'matched': [],
                            'errors': [f'fetch_failed: {e}']})
    return results


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== InStock 策略运行器自检 ===')
    print(f'已加载策略: {len(STRATEGIES)} 套')
    for sid, meta in STRATEGIES.items():
        print(f"  [{meta['category']}] {sid}: {meta['cn']} (需≥{meta['min_days']}日)")

    from stock_data import StockDataFetcher
    f = StockDataFetcher()
    for code, name in [('600519', '茅台'), ('000670', '盈方微'), ('603459', '红板科技')]:
        df = f.get_stock_data(code, '2y')
        r = run_one(code, df, name=name)
        print(f"\n{name}({code}): 命中 {r['matched_count']}/{r['total_strategies']}, errors={len(r['errors'])}")
        for m in r['matched']:
            print(f"  ✅ [{m['category']}] {m['cn']}")
