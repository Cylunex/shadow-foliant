"""
缠论（缠中说禅）形态识别 — 自包含纯 pandas 实现

借鉴 SkillHub「缠论形态识别」skill 的算法链路（去包含 → 分型 → 笔 → 中枢 → 背驰 → 买卖点），
但**不依赖 czsc 库**，仅用 pandas/numpy 重写，零新增依赖，适配本项目行情格式。

输入：StockDataFetcher.get_stock_data() 返回的 DataFrame
      （index=Date(datetime)，列含 High/Low/Close/Open/Volume；大小写不敏感，内部自动归一）。

输出：analyze_chan(df, symbol) -> dict，含 当前方向 / 分型 / 笔 / 最近中枢 /
      背驰 / 买卖点(一二三类) / 中文摘要(喂给 LLM 技术分析师)。

注意：缠论实现流派众多，这里取常见简化口径，定位为「给 AI 提供结构化缠论上下文」，
      非精确交易系统；参数可调。
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional
import math


# =============================================================================
# 工具：列名归一
# =============================================================================
def _pick_ohlc(df) -> Optional[Dict[str, list]]:
    """从 DataFrame 提取 high/low/close/(open) 与日期，列名大小写不敏感。

    返回 {'dt':[...], 'high':[...], 'low':[...], 'close':[...]}；失败返回 None。
    """
    if df is None or not hasattr(df, 'columns') or len(df) == 0:
        return None
    cols = {str(c).lower(): c for c in df.columns}
    need = {}
    for k in ('high', 'low', 'close'):
        if k in cols:
            need[k] = cols[k]
        else:
            return None
    high = [float(x) for x in df[need['high']].tolist()]
    low = [float(x) for x in df[need['low']].tolist()]
    close = [float(x) for x in df[need['close']].tolist()]
    # 日期：优先 index，否则找 date 列
    try:
        dt = [str(getattr(d, 'date', lambda: d)()) if hasattr(d, 'date') else str(d)
              for d in df.index.tolist()]
    except Exception:
        dt = [str(i) for i in range(len(close))]
    return {'dt': dt, 'high': high, 'low': low, 'close': close}


# =============================================================================
# 1) 去包含处理
# =============================================================================
def merge_inclusion(highs: List[float], lows: List[float], dts: List[str]) -> List[Dict[str, Any]]:
    """K线去包含，返回合并后的 bar 列表 [{idx, dt, high, low}]。

    方向规则：当出现包含关系时，按当前走势方向取高/低：
      - 向上：高点取大、低点取大
      - 向下：高点取小、低点取小
    首两根无方向时按高点比较初始化方向。
    """
    n = len(highs)
    if n == 0:
        return []
    merged: List[Dict[str, Any]] = [{'idx': 0, 'dt': dts[0], 'high': highs[0], 'low': lows[0]}]
    direction = 1  # 1=up, -1=down，初始假定向上
    for i in range(1, n):
        h, l = highs[i], lows[i]
        last = merged[-1]
        lh, ll = last['high'], last['low']
        # 包含关系：一根完全含住另一根
        contained = (h <= lh and l >= ll) or (h >= lh and l <= ll)
        if contained:
            if direction == 1:
                new_high = max(h, lh)
                new_low = max(l, ll)
            else:
                new_high = min(h, lh)
                new_low = min(l, ll)
            # 取极值所在原始日期
            last['high'] = new_high
            last['low'] = new_low
            last['dt'] = dts[i] if (new_high == h or new_low == l) else last['dt']
            last['idx'] = i
        else:
            direction = 1 if h > lh else -1
            merged.append({'idx': i, 'dt': dts[i], 'high': h, 'low': l})
    return merged


# =============================================================================
# 2) 分型识别
# =============================================================================
def find_fractals(merged: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """在去包含后的 bar 上识别分型。

    顶分型：中间 bar 高点最高且低点也最高；底分型：中间 bar 高低点皆最低。
    返回 [{m_idx, idx, dt, kind:'top'/'bottom', price}]，price 为顶=high/底=low。
    """
    fx: List[Dict[str, Any]] = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b['high'] > a['high'] and b['high'] > c['high'] and b['low'] > a['low'] and b['low'] > c['low']:
            fx.append({'m_idx': i, 'idx': b['idx'], 'dt': b['dt'], 'kind': 'top', 'price': b['high']})
        elif b['low'] < a['low'] and b['low'] < c['low'] and b['high'] < a['high'] and b['high'] < c['high']:
            fx.append({'m_idx': i, 'idx': b['idx'], 'dt': b['dt'], 'kind': 'bottom', 'price': b['low']})
    return fx


# =============================================================================
# 3) 笔
# =============================================================================
def build_bis(fractals: List[Dict[str, Any]], min_gap: int = 4) -> List[Dict[str, Any]]:
    """由分型构建笔。

    规则（简化标准口径）：
      - 顶底交替；
      - 相邻成笔分型在去包含 bar 上的间隔 >= min_gap（默认 4，即中间至少独立 1 根）；
      - 顶必须高于前一底、底必须低于前一顶（保证笔有方向）。
    返回 [{start_dt, end_dt, direction:'up'/'down', low, high, start_price, end_price}]。
    """
    if not fractals:
        return []
    anchors: List[Dict[str, Any]] = [fractals[0]]
    for fx in fractals[1:]:
        last = anchors[-1]
        if fx['kind'] == last['kind']:
            # 同型，取更极端者替换（顶取更高，底取更低）
            if (fx['kind'] == 'top' and fx['price'] > last['price']) or \
               (fx['kind'] == 'bottom' and fx['price'] < last['price']):
                anchors[-1] = fx
            continue
        # 异型，检查间隔与方向有效性
        if fx['m_idx'] - last['m_idx'] < min_gap:
            # 间隔不足，若当前更极端则替换上一个同型锚点逻辑已处理；此处跳过
            continue
        if fx['kind'] == 'top' and fx['price'] <= last['price']:
            continue
        if fx['kind'] == 'bottom' and fx['price'] >= last['price']:
            continue
        anchors.append(fx)

    bis: List[Dict[str, Any]] = []
    for i in range(len(anchors) - 1):
        a, b = anchors[i], anchors[i + 1]
        direction = 'up' if a['kind'] == 'bottom' else 'down'
        bis.append({
            'start_dt': a['dt'], 'end_dt': b['dt'],
            'start_m_idx': a['m_idx'], 'end_m_idx': b['m_idx'],
            'start_idx': a['idx'], 'end_idx': b['idx'],
            'direction': direction,
            'start_price': a['price'], 'end_price': b['price'],
            'low': min(a['price'], b['price']),
            'high': max(a['price'], b['price']),
        })
    return bis


# =============================================================================
# 4) 中枢
# =============================================================================
def find_zhongshus(bis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """构建中枢：连续 >=3 笔的重叠区间。

    ZG = min(各笔 high)，ZD = max(各笔 low)，ZG > ZD 即存在重叠；
    后续笔若仍与 [ZD, ZG] 重叠则并入，扩展中枢。
    返回 [{ZD, ZG, start_dt, end_dt, bi_count}]。
    """
    zss: List[Dict[str, Any]] = []
    i = 0
    while i + 2 < len(bis):
        win = bis[i:i + 3]
        zg = min(b['high'] for b in win)
        zd = max(b['low'] for b in win)
        if zg > zd:
            j = i + 3
            while j < len(bis):
                if bis[j]['low'] <= zg and bis[j]['high'] >= zd:
                    j += 1
                else:
                    break
            zss.append({
                'ZD': round(zd, 3), 'ZG': round(zg, 3),
                'start_dt': bis[i]['start_dt'], 'end_dt': bis[j - 1]['end_dt'],
                'bi_count': j - i,
                'start_bi': i, 'end_bi': j - 1,
            })
            i = j
        else:
            i += 1
    return zss


# =============================================================================
# 5) 背驰（MACD 面积近似）
# =============================================================================
def _macd(close: List[float], fast=12, slow=26, signal=9):
    """返回 (dif, dea, hist) 三个等长列表。纯 pandas ewm 实现。"""
    import pandas as pd
    s = pd.Series(close, dtype='float64')
    ema_f = s.ewm(span=fast, adjust=False).mean()
    ema_s = s.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif.tolist(), dea.tolist(), hist.tolist()


def _bi_macd_area(hist: List[float], start_idx: int, end_idx: int, direction: str) -> float:
    """某一笔区间内 MACD 柱面积（向上笔取红柱正面积，向下笔取绿柱负面积绝对值）。"""
    lo, hi = min(start_idx, end_idx), max(start_idx, end_idx)
    seg = hist[lo:hi + 1]
    if not seg:
        return 0.0
    if direction == 'up':
        return float(sum(x for x in seg if x > 0))
    return float(abs(sum(x for x in seg if x < 0)))


def detect_divergence(bis: List[Dict[str, Any]], hist: List[float]) -> Optional[Dict[str, Any]]:
    """比较最近两个同向笔，判断背驰。

    底背驰：最近一个向下笔创新低，但 MACD 绿柱面积小于上一个向下笔 → 看涨。
    顶背驰：最近一个向上笔创新高，但 MACD 红柱面积小于上一个向上笔 → 看跌。
    """
    if len(bis) < 3:
        return None
    last = bis[-1]
    prev_same = None
    for b in reversed(bis[:-1]):
        if b['direction'] == last['direction']:
            prev_same = b
            break
    if prev_same is None:
        return None
    a_last = _bi_macd_area(hist, last['start_idx'], last['end_idx'], last['direction'])
    a_prev = _bi_macd_area(hist, prev_same['start_idx'], prev_same['end_idx'], prev_same['direction'])
    if last['direction'] == 'down':
        new_extreme = last['low'] < prev_same['low']
        if new_extreme and a_last < a_prev:
            return {'type': 'bottom', 'meaning': '底背驰(看涨)',
                    'macd_area_now': round(a_last, 3), 'macd_area_prev': round(a_prev, 3)}
    else:
        new_extreme = last['high'] > prev_same['high']
        if new_extreme and a_last < a_prev:
            return {'type': 'top', 'meaning': '顶背驰(看跌)',
                    'macd_area_now': round(a_last, 3), 'macd_area_prev': round(a_prev, 3)}
    return None


# =============================================================================
# 6) 买卖点
# =============================================================================
def detect_buy_sell_point(bis: List[Dict[str, Any]], zss: List[Dict[str, Any]],
                          divergence: Optional[Dict[str, Any]], last_close: float) -> Dict[str, Any]:
    """综合笔/中枢/背驰判定最近的一/二/三类买卖点。

    简化口径：
      一买：下跌末端出现底背驰。
      一卖：上涨末端出现顶背驰。
      二买：一买后的次级回调低点不破一买低点（最近向下笔低点 > 前一向下笔低点 且方向转上）。
      二卖：对称。
      三买：价格向上突破中枢后回踩不回到中枢内（最近向下笔低点 > 最近中枢 ZG）。
      三卖：价格向下跌破中枢后反抽不回中枢内（最近向上笔高点 < 最近中枢 ZD）。
    """
    res = {'signal': '无', 'confidence': 'low', 'reason': '未识别明确买卖点'}
    if not bis:
        return res
    last = bis[-1]

    # 一买/一卖（背驰）
    if divergence and divergence['type'] == 'bottom':
        return {'signal': '一买', 'confidence': 'medium', 'reason': '下跌笔底背驰'}
    if divergence and divergence['type'] == 'top':
        return {'signal': '一卖', 'confidence': 'medium', 'reason': '上涨笔顶背驰'}

    # 三买/三卖（突破中枢后回踩不回）
    if zss:
        zs = zss[-1]
        # 找最近向下笔与向上笔
        last_down = next((b for b in reversed(bis) if b['direction'] == 'down'), None)
        last_up = next((b for b in reversed(bis) if b['direction'] == 'up'), None)
        if last['direction'] == 'up' and last_down and last_down['low'] > zs['ZG'] and last_close > zs['ZG']:
            return {'signal': '三买', 'confidence': 'medium',
                    'reason': f"突破中枢[{zs['ZD']}~{zs['ZG']}]后回踩不破上沿"}
        if last['direction'] == 'down' and last_up and last_up['high'] < zs['ZD'] and last_close < zs['ZD']:
            return {'signal': '三卖', 'confidence': 'medium',
                    'reason': f"跌破中枢[{zs['ZD']}~{zs['ZG']}]后反抽不回下沿"}

    # 二买/二卖（次级回调不破前低/前高）
    downs = [b for b in bis if b['direction'] == 'down']
    ups = [b for b in bis if b['direction'] == 'up']
    if last['direction'] == 'up' and len(downs) >= 2:
        if downs[-1]['low'] > downs[-2]['low']:
            return {'signal': '二买', 'confidence': 'low', 'reason': '回调低点抬高，不破前低'}
    if last['direction'] == 'down' and len(ups) >= 2:
        if ups[-1]['high'] < ups[-2]['high']:
            return {'signal': '二卖', 'confidence': 'low', 'reason': '反弹高点降低，不破前高'}

    return res


# =============================================================================
# 主入口
# =============================================================================
def analyze_chan(df, symbol: str = '', min_gap: int = 4) -> Dict[str, Any]:
    """对单只标的的日线 DataFrame 做缠论分析，返回结构化结果。"""
    out: Dict[str, Any] = {'symbol': symbol, 'available': False, 'errors': []}
    data = _pick_ohlc(df)
    if data is None:
        out['errors'].append('无有效 OHLC 数据（需 High/Low/Close 列）')
        return out
    if len(data['close']) < 30:
        out['errors'].append(f"K线不足（{len(data['close'])}<30），缠论分析跳过")
        return out

    try:
        merged = merge_inclusion(data['high'], data['low'], data['dt'])
        fractals = find_fractals(merged)
        bis = build_bis(fractals, min_gap=min_gap)
        zss = find_zhongshus(bis)
        _, _, hist = _macd(data['close'])
        divergence = detect_divergence(bis, hist)
        last_close = data['close'][-1]
        bsp = detect_buy_sell_point(bis, zss, divergence, last_close)

        cur_dir = bis[-1]['direction'] if bis else 'unknown'
        cur_zs = zss[-1] if zss else None
        price_vs_zs = None
        if cur_zs:
            if last_close > cur_zs['ZG']:
                price_vs_zs = '中枢上方'
            elif last_close < cur_zs['ZD']:
                price_vs_zs = '中枢下方'
            else:
                price_vs_zs = '中枢内部'

        out.update({
            'available': True,
            'last_close': round(last_close, 3),
            'merged_bars': len(merged),
            'fractal_count': len(fractals),
            'bi_count': len(bis),
            'current_direction': '上行' if cur_dir == 'up' else ('下行' if cur_dir == 'down' else '不明'),
            'last_fractal': ({'kind': '顶' if fractals[-1]['kind'] == 'top' else '底',
                              'dt': fractals[-1]['dt'], 'price': round(fractals[-1]['price'], 3)}
                             if fractals else None),
            'recent_bis': [{
                'direction': '上' if b['direction'] == 'up' else '下',
                'start_dt': b['start_dt'], 'end_dt': b['end_dt'],
                'low': round(b['low'], 3), 'high': round(b['high'], 3),
            } for b in bis[-5:]],
            'zhongshu_count': len(zss),
            'latest_zhongshu': cur_zs,
            'price_vs_zhongshu': price_vs_zs,
            'divergence': divergence,
            'buy_sell_point': bsp,
        })
        out['summary'] = format_chan_summary(out)
    except Exception as e:
        out['errors'].append(f'缠论计算异常: {e}')
    return out


def format_chan_summary(r: Dict[str, Any]) -> str:
    """把缠论结果转成给 LLM 的中文摘要。"""
    if not r.get('available'):
        return '缠论：数据不足或不可用。'
    parts = [f"【缠论结构】当前走势{r['current_direction']}，共识别 {r['bi_count']} 笔、{r['zhongshu_count']} 个中枢。"]
    if r.get('last_fractal'):
        lf = r['last_fractal']
        parts.append(f"最近分型：{lf['kind']}分型 @ {lf['price']}（{lf['dt']}）。")
    if r.get('latest_zhongshu'):
        zs = r['latest_zhongshu']
        parts.append(f"最近中枢区间 [{zs['ZD']} ~ {zs['ZG']}]（{zs['bi_count']}笔），现价位于{r.get('price_vs_zhongshu')}。")
    if r.get('divergence'):
        d = r['divergence']
        parts.append(f"背驰：{d['meaning']}（本笔MACD面积{d['macd_area_now']} vs 前笔{d['macd_area_prev']}）。")
    bsp = r.get('buy_sell_point') or {}
    if bsp.get('signal') and bsp['signal'] != '无':
        parts.append(f"缠论买卖点：**{bsp['signal']}**（{bsp['reason']}，置信度{bsp['confidence']}）。")
    else:
        parts.append("缠论买卖点：暂无明确一/二/三类买卖点。")
    return ' '.join(parts)


if __name__ == '__main__':
    # 自测：构造一段含下跌-震荡-反弹的合成数据
    import math as _m
    highs, lows, closes, dts = [], [], [], []
    price = 100.0
    for i in range(120):
        # 前40跌、中40震荡、后40涨
        if i < 40:
            price -= 0.8 + _m.sin(i / 3) * 0.3
        elif i < 80:
            price += _m.sin(i / 2) * 0.6
        else:
            price += 0.7 + _m.sin(i / 3) * 0.3
        h = price + abs(_m.sin(i)) * 0.8 + 0.5
        l = price - abs(_m.cos(i)) * 0.8 - 0.5
        highs.append(h); lows.append(l); closes.append(price)

    import pandas as pd
    idx = pd.date_range('2026-01-01', periods=len(closes), freq='D')
    df = pd.DataFrame({'High': highs, 'Low': lows, 'Close': closes}, index=idx)
    res = analyze_chan(df, 'TEST')
    import json
    print(json.dumps({k: v for k, v in res.items() if k != 'recent_bis'},
                     ensure_ascii=False, indent=2))
    print("\nSUMMARY:", res.get('summary'))
