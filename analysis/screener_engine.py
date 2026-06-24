"""
统一选股引擎(自主实现 / 路径B)—— 选股漏斗 L1「海选」层。

输入一组条件(realname),AND 组合,对股票池逐只本地判定命中。
- LOCAL_TECH：用 K线 计算 MA/MACD/KDJ/RSI/BOLL/CCI/BIAS + 阶段(新高新低/放缩量) + 缠论(chan_theory) + 形态(pattern_recognition)
- LOCAL_DATA：用「中文区间」通用解析(如「总市值大于等于20亿小于等于50亿」)对 行情/基本/财务 数值判定
- EXTERNAL：本地难算的(DDE/分时/关注度/龙虎榜占比/增减持…)记为「未判定」,从 AND 中剔除并提示(不静默)

下游:海选结果 universe → multi_factor_screener.rank_topn() 精排。

注：定位「给项目一个可解释、可控、零外部依赖的选股漏斗」。判定口径取常见简化版,可调。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any, Tuple
import re
import pandas as pd
import numpy as np

from screener_conditions import find, LOCAL_TECH, LOCAL_DATA, EXTERNAL


# ============================================================================
# 指标计算
# ============================================================================
def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
def _sma(s, n): return s.rolling(n).mean()


def _features(df: pd.DataFrame) -> Dict[str, Any]:
    """从 K线 DataFrame(列 High/Low/Close/Volume，大小写不敏感)算出常用指标序列与快照。"""
    cols = {str(c).lower(): c for c in df.columns}
    c = pd.to_numeric(df[cols['close']], errors='coerce')
    h = pd.to_numeric(df[cols['high']], errors='coerce') if 'high' in cols else c
    l = pd.to_numeric(df[cols['low']], errors='coerce') if 'low' in cols else c
    v = pd.to_numeric(df[cols['volume']], errors='coerce') if 'volume' in cols else pd.Series(np.nan, index=c.index)
    f: Dict[str, Any] = {'close': c, 'high': h, 'low': l, 'vol': v, 'n': len(c)}
    for n in (5, 10, 20, 60):
        f[f'ma{n}'] = _sma(c, n)
    # MACD
    dif = _ema(c, 12) - _ema(c, 26); dea = _ema(dif, 9); hist = (dif - dea) * 2
    f['dif'], f['dea'], f['hist'] = dif, dea, hist
    # KDJ(9,3,3)
    low9 = l.rolling(9).min(); high9 = h.rolling(9).max()
    rsv = (c - low9) / (high9 - low9).replace(0, np.nan) * 100
    f['K'] = rsv.ewm(alpha=1/3, adjust=False).mean()
    f['D'] = f['K'].ewm(alpha=1/3, adjust=False).mean()
    f['J'] = 3 * f['K'] - 2 * f['D']
    # RSI(14)
    delta = c.diff(); up = delta.clip(lower=0); dn = (-delta).clip(lower=0)
    rs = up.ewm(alpha=1/14, adjust=False).mean() / dn.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
    f['rsi'] = 100 - 100 / (1 + rs)
    # BOLL(20,2)
    mid = _sma(c, 20); std = c.rolling(20).std(ddof=0)
    f['boll_mid'], f['boll_up'], f['boll_dn'] = mid, mid + 2 * std, mid - 2 * std
    f['boll_width'] = (f['boll_up'] - f['boll_dn']) / mid
    # CCI(14)
    tp = (h + l + c) / 3
    f['cci'] = (tp - _sma(tp, 14)) / (0.015 * tp.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True))
    # BIAS
    for n in (6, 12, 24):
        f[f'bias{n}'] = (c - _sma(c, n)) / _sma(c, n) * 100
    return f


def _cross_up(a: pd.Series, b: pd.Series) -> bool:
    """a 上穿 b（最后一根）。"""
    if len(a) < 2:
        return False
    try:
        return bool(a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1])
    except Exception:
        return False


def _last(s: pd.Series, i: int = -1) -> float:
    try:
        x = float(s.iloc[i]);  return x if not np.isnan(x) else float('nan')
    except Exception:
        return float('nan')


# ============================================================================
# LOCAL_TECH 判定（realname -> 函数(features)->bool）
# ============================================================================
def _eval_tech(df: pd.DataFrame) -> Dict[str, bool]:
    """计算该股命中的所有本地技术/阶段条件,返回 {realname: True}。"""
    if df is None or len(df) < 30:
        return {}
    f = _features(df)
    c, v = f['close'], f['vol']
    out: Dict[str, bool] = {}

    def put(name, cond):
        try:
            out[name] = bool(cond)
        except Exception:
            pass

    # 均线
    ma5, ma10, ma20, ma60 = f['ma5'], f['ma10'], f['ma20'], f['ma60']
    put('多头排列', _last(ma5) > _last(ma10) > _last(ma20) > _last(ma60))
    put('均线粘合', (np.nanmax([_last(ma5), _last(ma10), _last(ma20)]) /
                     np.nanmin([_last(ma5), _last(ma10), _last(ma20)]) - 1) < 0.02)
    put('股价站上5日线', _last(c) > _last(ma5))
    put('5日线金叉10日线', _cross_up(ma5, ma10))
    # MACD
    put('macd金叉', _cross_up(f['dif'], f['dea']))
    put('macd零轴金叉', _cross_up(f['dif'], f['dea']) and _last(f['dif']) > 0)
    put('macd买入信号', _last(f['hist']) > 0 and _last(f['hist'], -2) <= 0)
    # KDJ
    put('kdj金叉', _cross_up(f['K'], f['D']))
    put('kdj买入信号', _cross_up(f['K'], f['D']) and _last(f['K']) < 30)
    put('kdj拐头向上', _last(f['J']) > _last(f['J'], -2))
    # RSI
    put('rsi超卖', _last(f['rsi']) < 30)
    put('rsi买入信号', _last(f['rsi']) > 30 and _last(f['rsi'], -2) <= 30)
    put('rsi金叉', _cross_up(f['rsi'], _sma(f['rsi'], 6)))
    # BOLL
    put('boll突破下轨', _cross_up(c, f['boll_dn']))
    put('boll突破中轨', _cross_up(c, f['boll_mid']))
    put('boll突破上轨', _cross_up(c, f['boll_up']))
    put('boll开口张开', _last(f['boll_width']) > _last(f['boll_width'], -5))
    # CCI
    put('cci超卖', _last(f['cci']) < -100)
    put('cci买入信号', _last(f['cci']) > -100 and _last(f['cci'], -2) <= -100)
    put('cci拐头向上', _last(f['cci']) > _last(f['cci'], -2))
    # BIAS
    put('bias超卖', _last(f['bias6']) < -5)
    # 阶段表现：创新高/新低
    for n in (20, 60, 120):
        if f['n'] > n:
            put(f'股价创{n}日新高', _last(c) >= float(c.iloc[-n:].max()))
            put(f'股价创{n}日新低', _last(c) <= float(c.iloc[-n:].min()))
    put('股价创历史新高', _last(c) >= float(c.max()))
    put('股价创历史新低', _last(c) <= float(c.min()))
    # 放量/缩量(最近N日 vs 前N日均量)
    if v.notna().sum() > 25:
        for n in (10, 20, 60):
            if f['n'] > 2 * n:
                recent = v.iloc[-n:].mean(); base = v.iloc[-2*n:-n].mean()
                if base and not np.isnan(base):
                    put(f'最近{n}日放量', recent > base * 1.5)
                    put(f'最近{n}日缩量', recent < base * 0.7)
        put('放量', _last(v) > v.iloc[-6:-1].mean() * 1.5 if f['n'] > 6 else False)
        put('缩量', _last(v) < v.iloc[-6:-1].mean() * 0.7 if f['n'] > 6 else False)
    # 平台整理/突破(N日振幅收窄 / 突破N日高点)
    for n in (10, 20, 60):
        if f['n'] > n:
            seg = c.iloc[-n:]
            box = (seg.max() / seg.min() - 1) if seg.min() else 1
            put(f'平台整理{n}日', box < 0.10)
            put(f'突破平台{n}日', _last(c) > float(c.iloc[-n-1:-1].max()))

    return {k: True for k, val in out.items() if val}


def _eval_chan(df: pd.DataFrame) -> Dict[str, bool]:
    """缠论买卖点(本地独有条件:缠论一买/二买/三买/底背驰)。"""
    try:
        from chan_theory import analyze_chan
        r = analyze_chan(df)
        if not r.get('available'):
            return {}
        out = {}
        bsp = (r.get('buy_sell_point') or {}).get('signal')
        if bsp and bsp != '无':
            out[f'缠论{bsp}'] = True       # 缠论一买/二买/三买/一卖...
        if (r.get('divergence') or {}).get('type') == 'bottom':
            out['缠论底背驰'] = True
        return out
    except Exception:
        return {}


def _eval_pattern(df: pd.DataFrame) -> Dict[str, bool]:
    """K线形态(复用 pattern_recognition,按中文名匹配)。"""
    try:
        from pattern_recognition import PatternDetector
        det = PatternDetector()
        if not getattr(det, 'available', False) or len(df) < 120:
            return {}
        raw = det.detect_all(df, lookback=5)
        out = {}
        for pid, r in raw.items():
            if isinstance(r, dict) and r.get('found') and r.get('name'):
                out[r['name']] = True
        return out
    except Exception:
        return {}


# ============================================================================
# LOCAL_DATA：中文数值区间通用解析
# ============================================================================
_UNIT = {'亿': 1e8, '万': 1e4, '%': 0.01}


def _num(tok: str) -> Optional[float]:
    m = re.search(r'(-?\d+(?:\.\d+)?)', tok)
    if not m:
        return None
    val = float(m.group(1))
    for u, mul in _UNIT.items():
        if u in tok:
            val *= mul
            break
    return val


def parse_range(realname: str) -> Optional[Tuple[str, float, float]]:
    """把「市盈率大于等于0小于等于25」「总市值小于20亿」「涨跌幅大于5%」解析为 (metric, lo, hi)。

    lo/hi 用 ±inf 表示开放端。无法解析返回 None。
    """
    INF = float('inf')
    # 区间：A大于等于x小于等于y  /  Ax~y 形式已在注册表用中文，故只解析中文比较
    m = re.match(r'^(.+?)大于等于(.+?)小于等于(.+)$', realname)
    if m:
        lo, hi = _num(m.group(2)), _num(m.group(3))
        if lo is not None and hi is not None:
            return (m.group(1), lo, hi)
    m = re.match(r'^(.+?)小于等于(.+)$', realname)
    if m:
        hi = _num(m.group(2))
        if hi is not None:
            return (m.group(1), -INF, hi)
    m = re.match(r'^(.+?)大于等于(.+)$', realname)
    if m:
        lo = _num(m.group(2))
        if lo is not None:
            return (m.group(1), lo, INF)
    m = re.match(r'^(.+?)小于(.+)$', realname)
    if m:
        hi = _num(m.group(2))
        if hi is not None:
            return (m.group(1), -INF, hi)
    m = re.match(r'^(.+?)大于(.+)$', realname)
    if m:
        lo = _num(m.group(2))
        if lo is not None:
            return (m.group(1), lo, INF)
    return None


# metric 中文名 → 取值器(从 因子/快照/K线 取数)
def _metric_value(metric: str, df: pd.DataFrame, factors: Dict[str, Any], spot: Dict[str, Any]) -> Optional[float]:
    cols = {str(c).lower(): c for c in df.columns} if df is not None else {}
    close = float(pd.to_numeric(df[cols['close']], errors='coerce').dropna().iloc[-1]) if 'close' in cols else None
    table = {
        '股价': close,
        '市盈率': factors.get('forward_pe') or factors.get('pe'),
        '市净率': factors.get('pb'),
        '净资产收益率': factors.get('roe'),
        '净利润增长率': factors.get('net_profit_growth'),
        '资产负债率': factors.get('debt_ratio'),
        '总市值': spot.get('总市值'),
        '流通市值': spot.get('流通市值'),
        '换手率': spot.get('换手率'),
        '量比': spot.get('量比'),
        '涨跌幅': spot.get('涨跌幅'),
        '振幅': spot.get('振幅'),
    }
    val = table.get(metric)
    try:
        return float(val) if val is not None and not (isinstance(val, float) and np.isnan(val)) else None
    except Exception:
        return None


# ============================================================================
# 主流程：screen
# ============================================================================
def screen(conditions: List[str], universe: List[str],
           period: str = '1y', verbose: bool = True) -> Dict[str, Any]:
    """对 universe 逐只判定，返回命中所有(可判定)条件的股票。

    Args:
        conditions: 条件 realname 列表(AND)。
        universe:   股票池(代码列表)。
    Returns:
        {'hits':[...], 'evaluated':[...], 'skipped_external':[...], 'universe_size':n}
    """
    # 分类条件
    tech_conds, data_conds, skipped = [], [], []
    for q in conditions:
        cond = find(q)
        rv = cond.resolver if cond else EXTERNAL
        if rv == LOCAL_TECH:
            tech_conds.append(q)
        elif rv == LOCAL_DATA:
            data_conds.append(q)
        else:
            skipped.append(q)
    # 缠论/形态为本地独有(注册表外)
    for q in conditions:
        if q.startswith('缠论') and q not in tech_conds:
            tech_conds.append(q)

    if skipped and verbose:
        print(f"⚠ 以下条件本地无法判定(EXTERNAL),已从筛选中剔除: {skipped}")

    from stock_data import StockDataFetcher
    from fundamental_scoring import collect_factors
    fetcher = StockDataFetcher()

    hits = []
    for i, sym in enumerate(universe):
        if verbose and i % 25 == 0:
            print(f"  筛选 {i}/{len(universe)} ...")
        try:
            df = fetcher.get_stock_data(sym, period, adjust='qfq')  # 条件选股(技术形态)用前复权
            if isinstance(df, dict) or df is None or len(df) < 30:
                continue
            ok = True
            # 技术/阶段/缠论/形态
            if tech_conds:
                avail = {}
                avail.update(_eval_tech(df))
                if any(x.startswith('缠论') for x in tech_conds):
                    avail.update(_eval_chan(df))
                if any(find(x) and find(x).group == '形态' for x in tech_conds):
                    avail.update(_eval_pattern(df))
                for q in tech_conds:
                    if not avail.get(q, False):
                        ok = False; break
            # 数值区间(行情/基本/财务)
            if ok and data_conds:
                factors = {}
                try:
                    factors = collect_factors(sym) or {}
                except Exception:
                    pass
                spot = {}  # 实时快照(量比/换手/市值)可后续接 akshare spot;缺失则该条跳过
                for q in data_conds:
                    pr = parse_range(q)
                    if not pr:
                        continue  # 无法解析 → 不作约束
                    metric, lo, hi = pr
                    val = _metric_value(metric, df, factors, spot)
                    if val is None:
                        continue  # 取不到该指标 → 不作约束(避免误杀)
                    if not (lo <= val <= hi):
                        ok = False; break
            if ok:
                hits.append(sym)
        except Exception:
            continue

    return {
        'hits': hits,
        'universe_size': len(universe),
        'evaluated_conditions': tech_conds + data_conds,
        'skipped_external': skipped,
    }


# ============================================================================
# 内置策略配方(条件组合)
# ============================================================================
RECIPES: Dict[str, List[str]] = {
    '主升浪起涨': ['多头排列', 'macd零轴金叉', '换手率大于等于3%小于等于5%', '总市值大于等于20亿小于等于50亿'],
    '超跌反弹': ['rsi超卖', 'kdj买入信号', '股价创60日新低', '最近20日缩量'],
    '强势突破': ['突破平台20日', '放量', '股价创60日新高'],
    '低估值蓝筹': ['市盈率大于等于0小于等于25', '市净率大于等于0小于等于1', '净资产收益率大于15%'],
    '缠论一买': ['缠论一买', '最近20日缩量'],
    '均线金叉起步': ['5日线金叉10日线', '股价站上5日线', 'macd金叉'],
}


def screen_recipe(recipe: str, universe: List[str], **kw) -> Dict[str, Any]:
    """按内置配方选股。"""
    conds = RECIPES.get(recipe)
    if not conds:
        return {'error': f'未知配方: {recipe}', 'available': list(RECIPES.keys())}
    res = screen(conds, universe, **kw)
    res['recipe'] = recipe
    res['conditions'] = conds
    return res


if __name__ == '__main__':
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    # 离线自测：合成一只「多头排列+放量+创新高」的票
    import math
    n = 160
    base = [50 + i * 0.25 + math.sin(i / 5) * 1.5 for i in range(n)]  # 稳步上行
    df = pd.DataFrame({
        'High': [b + 1 for b in base], 'Low': [b - 1 for b in base],
        'Close': base, 'Open': base,
        'Volume': [1e6 * (2.0 if i >= n - 3 else 1.0) for i in range(n)],  # 末尾放量
    }, index=pd.date_range('2025-01-01', periods=n, freq='D'))

    tech = _eval_tech(df)
    print("命中本地技术/阶段条件(部分):")
    for k in ['多头排列', '股价站上5日线', '5日线金叉10日线', 'macd金叉', '放量',
              '股价创20日新高', '股价创60日新高', '突破平台20日']:
        print(f"  {k}: {tech.get(k, False)}")
    print("\nparse_range 测试:")
    for s in ['总市值大于等于20亿小于等于50亿', '市盈率小于0', '换手率大于5%', '股价大于等于5元小于等于10元']:
        print(f"  {s} -> {parse_range(s)}")
    print("\n缠论判定:", _eval_chan(df))
    print("内置配方:", list(RECIPES.keys()))
