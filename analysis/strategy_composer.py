# -*- coding: utf-8 -*-
"""策略组合器 — 用条件积木(gene blocks)动态产出全新策略

设计(2026-06-12,策略基因组三层进化的"产新"层):
  - 14 个参数化条件积木(趋势/突破/量能/动量/回踩/波动率),每个有自己的参数空间
  - 一个"组合策略" = 2~4 个积木的 AND 组合(基因 genes=[{'b':积木id,'p':{参数}}...])
  - 随机生成 random_genes / 变异 mutate_genes(参数扰动+结构增删换) / 交叉 crossover_genes
  - check_composed 与 InStock 策略同签名 → backtest_engine 直接回测,胜出者进 live 选股

本模块零数据库依赖(被 strategy_genome 调用做进化;不得反向 import genome 防循环)。
"""

import random
from typing import Any, Dict, List, Optional

import numpy as np
try:
    import talib as tl
except ImportError:
    # 生产环境可能未装 talib(C 库)→ 用 pandas 等价实现兜底,COMPOSED 策略照常工作,绝不因缺库整体崩
    import pandas as _pd

    class _TLShim:
        @staticmethod
        def MA(closes, timeperiod):
            return _pd.Series(closes).rolling(int(timeperiod)).mean().to_numpy()

        @staticmethod
        def RSI(closes, timeperiod=14):
            s = _pd.Series(closes)
            d = s.diff()
            up = d.clip(lower=0).rolling(int(timeperiod)).mean()
            dn = (-d.clip(upper=0)).rolling(int(timeperiod)).mean()
            rs = up / dn.replace(0, np.nan)
            return (100 - 100 / (1 + rs)).to_numpy()

        @staticmethod
        def MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9):
            s = _pd.Series(closes)
            dif = s.ewm(span=fastperiod, adjust=False).mean() - s.ewm(span=slowperiod, adjust=False).mean()
            dea = dif.ewm(span=signalperiod, adjust=False).mean()
            return dif.to_numpy(), dea.to_numpy(), ((dif - dea) * 2).to_numpy()

    tl = _TLShim()

# ══════════════════════════════════════════════════════════
#  条件积木库 — id: (中文名模板, 参数空间{name:(lo,hi,step,desc)}, 判定函数)
#  判定函数签名: fn(d, p) -> bool
#    d: 已按日期截断的标准化 DataFrame(date/open/high/low/close/volume/p_change),最后一行=当日
#    p: 参数 dict
# ══════════════════════════════════════════════════════════

def _ma(closes, n):
    if len(closes) < n:
        return None
    return tl.MA(closes, timeperiod=int(n))


def _b_ma_above(d, p):
    n = int(p['period'])
    ma = _ma(d['close'].values, n)
    return ma is not None and not np.isnan(ma[-1]) and float(d.iloc[-1]['close']) > ma[-1]


def _b_ma_rising(d, p):
    n, lb = int(p['period']), int(p['lookback'])
    ma = _ma(d['close'].values, n)
    if ma is None or len(ma) <= lb or np.isnan(ma[-1]) or np.isnan(ma[-1 - lb]):
        return False
    return ma[-1] > ma[-1 - lb]


def _b_macd_bull(d, p):
    closes = d['close'].values
    if len(closes) < 35:
        return False
    dif, dea, _ = tl.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
    return not np.isnan(dif[-1]) and not np.isnan(dea[-1]) and dif[-1] > dea[-1]


def _b_new_high(d, p):
    n = int(p['n'])
    closes = d['close'].values
    if len(closes) < n:
        return False
    return float(closes[-1]) >= float(np.max(closes[-n:]))


def _b_range_break(d, p):
    n = int(p['n'])
    if len(d) < n + 1:
        return False
    prev_high = float(d['high'].values[-(n + 1):-1].max())
    return float(d.iloc[-1]['close']) > prev_high


def _vol_ma5_prev(d):
    vols = d['volume'].values
    if len(vols) < 6:
        return None
    m = float(np.mean(vols[-6:-1]))
    return m if m > 0 else None


def _b_vol_surge(d, p):
    m = _vol_ma5_prev(d)
    return m is not None and float(d.iloc[-1]['volume']) > p['k'] * m


def _b_vol_shrink(d, p):
    m = _vol_ma5_prev(d)
    return m is not None and float(d.iloc[-1]['volume']) < p['k'] * m


def _b_up_day(d, p):
    return float(d.iloc[-1]['p_change']) >= p['x']


def _b_consec_up(d, p):
    n = int(p['n'])
    if len(d) < n:
        return False
    tail = d.tail(n)
    return bool((tail['close'].values > tail['open'].values).all())


def _rsi14(d):
    closes = d['close'].values
    if len(closes) < 20:
        return None
    r = tl.RSI(closes, timeperiod=14)
    return None if np.isnan(r[-1]) else float(r[-1])


def _b_rsi_below(d, p):
    r = _rsi14(d)
    return r is not None and r < p['x']


def _b_rsi_above(d, p):
    r = _rsi14(d)
    return r is not None and r > p['x']


def _b_near_high_pullback(d, p):
    """距 n 日最高收盘回撤 ≤ max_pct%(强势整理,没破位)且当日未创新高(是回踩不是突破)"""
    n = int(p['n'])
    closes = d['close'].values
    if len(closes) < n:
        return False
    hi = float(np.max(closes[-n:]))
    last = float(closes[-1])
    if hi <= 0 or last >= hi:
        return False
    return (hi - last) / hi * 100 <= p['max_pct']


def _b_above_ma_band(d, p):
    """收盘在 MA ~ MA*(1+band%) 之间(回踩均线支撑附近)"""
    n = int(p['period'])
    ma = _ma(d['close'].values, n)
    if ma is None or np.isnan(ma[-1]) or ma[-1] <= 0:
        return False
    last = float(d.iloc[-1]['close'])
    return ma[-1] <= last <= ma[-1] * (1 + p['band'] / 100)


def _b_atr_low(d, p):
    n = int(p['n'])
    pc = d['p_change'].values
    if len(pc) < n:
        return False
    return float(np.mean(np.abs(pc[-n:]))) < p['x']


BLOCKS: Dict[str, Dict[str, Any]] = {
    'ma_above':      {'cn': 'MA{period}上方',       'space': {'period': (5, 120, 5, 'MA周期')},                                  'fn': _b_ma_above},
    'ma_rising':     {'cn': 'MA{period}走升',       'space': {'period': (5, 120, 5, 'MA周期'), 'lookback': (3, 20, 1, '对比天数')}, 'fn': _b_ma_rising},
    'macd_bull':     {'cn': 'MACD多头',             'space': {},                                                                  'fn': _b_macd_bull},
    'new_high':      {'cn': '创{n}日新高',          'space': {'n': (10, 120, 5, '回溯天数')},                                     'fn': _b_new_high},
    'range_break':   {'cn': '破{n}日箱体',          'space': {'n': (5, 60, 5, '回溯天数')},                                       'fn': _b_range_break},
    'vol_surge':     {'cn': '放量{k}x',             'space': {'k': (1.2, 5.0, 0.2, '量比下限')},                                  'fn': _b_vol_surge},
    'vol_shrink':    {'cn': '缩量{k}x',             'space': {'k': (0.4, 0.9, 0.05, '量比上限')},                                 'fn': _b_vol_shrink},
    'up_day':        {'cn': '日涨≥{x}%',            'space': {'x': (1.0, 7.0, 0.5, '最小涨幅%')},                                 'fn': _b_up_day},
    'consec_up':     {'cn': '连{n}阳',              'space': {'n': (2, 5, 1, '连阳天数')},                                        'fn': _b_consec_up},
    'rsi_below':     {'cn': 'RSI<{x}',              'space': {'x': (20, 45, 1, 'RSI上限')},                                       'fn': _b_rsi_below},
    'rsi_above':     {'cn': 'RSI>{x}',              'space': {'x': (50, 80, 2, 'RSI下限')},                                       'fn': _b_rsi_above},
    'near_high_pullback': {'cn': '距{n}日高点≤{max_pct}%', 'space': {'n': (20, 120, 10, '回溯天数'), 'max_pct': (3, 25, 1, '最大回撤%')}, 'fn': _b_near_high_pullback},
    'above_ma_band': {'cn': '踩MA{period}+{band}%内', 'space': {'period': (10, 90, 5, 'MA周期'), 'band': (1.0, 8.0, 0.5, '带宽%')}, 'fn': _b_above_ma_band},
    'atr_low':       {'cn': '{n}日振幅<{x}%',       'space': {'n': (5, 30, 5, '回溯天数'), 'x': (1.5, 6.0, 0.5, '振幅上限%')},     'fn': _b_atr_low},
}

# 互斥积木(同时出现没有意义/自相矛盾)
_EXCLUSIVE = [
    {'vol_surge', 'vol_shrink'},
    {'rsi_below', 'rsi_above'},
    {'new_high', 'near_high_pullback'},
]

MIN_BLOCKS, MAX_BLOCKS = 2, 4
MIN_HISTORY = 130  # 组合策略统一要求的最少K线数


# ══════════════════════════════════════════════════════════
#  基因操作
# ══════════════════════════════════════════════════════════

def _quantize(val, lo, hi, step):
    if step:
        val = round((val - lo) / step) * step + lo
    val = max(lo, min(hi, val))
    if float(step).is_integer() and float(lo).is_integer() and float(hi).is_integer():
        return int(round(val))
    return round(val, 6)


def _random_params(block_id: str) -> Dict[str, Any]:
    out = {}
    for k, (lo, hi, step, _desc) in BLOCKS[block_id]['space'].items():
        out[k] = _quantize(lo + random.random() * (hi - lo), lo, hi, step)
    return out


def _conflicts(block_ids: List[str]) -> bool:
    s = set(block_ids)
    return any(len(s & ex) > 1 for ex in _EXCLUSIVE)


def random_genes(n_blocks: int = None) -> List[Dict[str, Any]]:
    """随机生成一套组合策略基因"""
    n = n_blocks or random.randint(MIN_BLOCKS, 3)
    for _ in range(50):
        ids = random.sample(list(BLOCKS), n)
        if not _conflicts(ids):
            return [{'b': b, 'p': _random_params(b)} for b in ids]
    # 兜底
    return [{'b': 'ma_above', 'p': _random_params('ma_above')},
            {'b': 'vol_surge', 'p': _random_params('vol_surge')}]


def mutate_genes(genes: List[Dict[str, Any]], strength: float = 0.3,
                 p_struct: float = 0.3) -> List[Dict[str, Any]]:
    """变异:参数扰动 + 概率性结构变化(增/删/换一个积木)"""
    new = [{'b': g['b'], 'p': dict(g['p'])} for g in genes if g.get('b') in BLOCKS]
    if not new:
        return random_genes()

    # 参数扰动
    for g in new:
        for k, (lo, hi, step, _desc) in BLOCKS[g['b']]['space'].items():
            base = float(g['p'].get(k, lo))
            delta = (hi - lo) * strength * (random.random() * 2 - 1)
            g['p'][k] = _quantize(base + delta, lo, hi, step)

    # 结构变化
    if random.random() < p_struct:
        op = random.choice(['add', 'remove', 'swap'])
        ids = [g['b'] for g in new]
        if op == 'add' and len(new) < MAX_BLOCKS:
            cands = [b for b in BLOCKS if b not in ids and not _conflicts(ids + [b])]
            if cands:
                b = random.choice(cands)
                new.append({'b': b, 'p': _random_params(b)})
        elif op == 'remove' and len(new) > MIN_BLOCKS:
            new.pop(random.randrange(len(new)))
        elif op == 'swap':
            i = random.randrange(len(new))
            rest = [g['b'] for j, g in enumerate(new) if j != i]
            cands = [b for b in BLOCKS if b not in rest and not _conflicts(rest + [b])]
            if cands:
                b = random.choice(cands)
                new[i] = {'b': b, 'p': _random_params(b)}
    return new


def crossover_genes(g1: List[Dict], g2: List[Dict]) -> List[Dict[str, Any]]:
    """交叉:两套基因的积木混选(同积木随机取一方参数),去冲突,2~4 个"""
    pool: Dict[str, Dict] = {}
    for g in list(g1) + list(g2):
        b = g.get('b')
        if b not in BLOCKS:
            continue
        if b not in pool or random.random() < 0.5:
            pool[b] = {'b': b, 'p': dict(g['p'])}
    ids = list(pool)
    random.shuffle(ids)
    picked: List[str] = []
    for b in ids:
        if len(picked) >= MAX_BLOCKS:
            break
        if not _conflicts(picked + [b]):
            picked.append(b)
    if len(picked) < MIN_BLOCKS:
        return random_genes()
    return [pool[b] for b in picked]


def genes_cn(genes: List[Dict[str, Any]], max_len: int = 30) -> str:
    """基因 → 可读中文名,如 'MA20上方+破20日箱体+放量1.8x'(截断到 max_len,适配 VARCHAR(32))"""
    parts = []
    for g in genes:
        meta = BLOCKS.get(g.get('b'))
        if not meta:
            continue
        try:
            parts.append(meta['cn'].format(**g.get('p', {})))
        except (KeyError, IndexError):
            parts.append(g['b'])
    s = '+'.join(parts) or 'composed'
    return s[:max_len]


# ══════════════════════════════════════════════════════════
#  判定入口 — 与 InStock 策略同签名,可被 backtest_engine 逐日回放
# ══════════════════════════════════════════════════════════

def check_composed(code_name, data, date=None, genes: List[Dict[str, Any]] = None):
    """组合策略判定:全部积木 AND。data 需为标准化 DataFrame(同 instock_strategy_runner._normalize_df)"""
    if not genes:
        return False
    if date is None:
        end_date = code_name[0]
    else:
        end_date = date.strftime('%Y-%m-%d')
    if end_date is not None:
        data = data.loc[data['date'] <= end_date]
    if len(data.index) < MIN_HISTORY:
        return False
    for g in genes:
        meta = BLOCKS.get(g.get('b'))
        if meta is None:
            return False
        try:
            if not meta['fn'](data, g.get('p', {})):
                return False
        except Exception:
            return False
    return True


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 策略组合器自检 ===')
    print(f'积木库: {len(BLOCKS)} 个')
    random.seed(7)
    for i in range(3):
        g = random_genes()
        print(f'\n随机策略{i + 1}: {genes_cn(g)}')
        print(f'  基因: {g}')
        m = mutate_genes(g)
        print(f'  变异: {genes_cn(m)}')
    c = crossover_genes(random_genes(), random_genes())
    print(f'\n交叉: {genes_cn(c)}')

    # 真实数据冒烟
    try:
        import _bootstrap  # noqa: F401
        from stock_data import StockDataFetcher
        from selection.instock_strategy_runner import _normalize_df
        df = _normalize_df(StockDataFetcher().get_stock_data('600519', '2y'))
        hits = 0
        for i in range(20):
            g = random_genes()
            if check_composed((df['date'].iloc[-1], '茅台'), df, genes=g):
                hits += 1
                print(f'  ✅ 茅台命中: {genes_cn(g)}')
        print(f'\n茅台随机20策略命中 {hits} 个(有命中说明判定链通)')
    except Exception as e:
        print(f'(真实数据冒烟跳过: {e})')
