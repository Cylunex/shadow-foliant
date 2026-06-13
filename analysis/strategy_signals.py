"""策略信号(纯函数,借鉴 daily_stock_analysis 的策略 YAML)。

目的:给选股/持仓加"反转+量能确认"的稳健信号(治"低价+跌幅+低位 接飞刀"),
并提供行情阶段(regime)判定供决策/策略路由。

输入统一:DataFrame 含列 date/open/high/low/close/volume(小写),按时间升序;
返回 dict {signal:bool, reason:str, ...}。数据不足/异常返回 {signal:False, reason:...}。
"""

from typing import Dict
import pandas as pd


def _prep(df: pd.DataFrame, need: int = 20):
    if df is None or len(df) < need:
        return None
    d = df.copy()
    # 兼容大写列名
    ren = {c: c.lower() for c in d.columns if c.lower() in ('open', 'high', 'low', 'close', 'volume')}
    d = d.rename(columns=ren)
    for c in ('open', 'high', 'low', 'close', 'volume'):
        if c not in d.columns:
            return None
        d[c] = pd.to_numeric(d[c], errors='coerce')
    return d.dropna(subset=['close']).reset_index(drop=True)


def shrink_pullback(df: pd.DataFrame) -> Dict:
    """缩量回踩:上升趋势(MA5>MA10>MA20)中缩量回踩 MA5(±1%)或 MA10(±2%) → 稳健低吸(不接飞刀)。"""
    d = _prep(df, 25)
    if d is None:
        return {'signal': False, 'reason': '数据不足'}
    c = d['close']
    ma5, ma10, ma20 = c.rolling(5).mean(), c.rolling(10).mean(), c.rolling(20).mean()
    last, m5, m10, m20 = c.iloc[-1], ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
    bullish = m5 > m10 > m20
    near5 = abs(last - m5) / m5 <= 0.01
    near10 = abs(last - m10) / m10 <= 0.02
    v = d['volume']
    shrink = v.iloc[-1] < v.iloc[-6:-1].mean() * 0.7 if len(v) >= 6 else False
    sig = bool(bullish and (near5 or near10) and shrink)
    return {'signal': sig, 'reason': ('多头排列+缩量回踩' + ('MA5' if near5 else 'MA10')) if sig
            else f'未满足(多头={bullish},回踩={near5 or near10},缩量={shrink})'}


def bottom_volume(df: pd.DataFrame) -> Dict:
    """底部放量:近20日自高点跌>15% + 当日放量(>3×近5日均量) + 收阳 → 反转确认(非接飞刀)。"""
    d = _prep(df, 25)
    if d is None:
        return {'signal': False, 'reason': '数据不足'}
    hh20 = d['high'].iloc[-20:].max()
    last = d['close'].iloc[-1]
    decline = (hh20 - last) / hh20 if hh20 else 0
    v = d['volume']
    surge = v.iloc[-1] > v.iloc[-6:-1].mean() * 3 if len(v) >= 6 else False
    yang = d['close'].iloc[-1] > d['open'].iloc[-1]
    sig = bool(decline > 0.15 and surge and yang)
    return {'signal': sig, 'reason': (f'自高点跌{decline*100:.0f}%+放量+收阳=底部反转') if sig
            else f'未满足(跌幅={decline*100:.0f}%,放量={surge},收阳={yang})',
            'decline_pct': round(decline * 100, 1)}


def emotion_top_warning(df: pd.DataFrame) -> Dict:
    """情绪顶预警(持仓用):乖离 MA20 过大(>8%) + 近5日放量(>前期2×)→ 过热,谨慎/减仓。"""
    d = _prep(df, 30)
    if d is None:
        return {'signal': False, 'reason': '数据不足'}
    c = d['close']
    ma20 = c.rolling(20).mean().iloc[-1]
    last = c.iloc[-1]
    bias = (last - ma20) / ma20 if ma20 else 0
    v = d['volume']
    recent_v = v.iloc[-5:].mean()
    base_v = v.iloc[-25:-5].mean() if len(v) >= 25 else v.mean()
    hot = recent_v > base_v * 2 if base_v else False
    sig = bool(bias > 0.08 and hot)
    return {'signal': sig, 'reason': (f'乖离MA20 +{bias*100:.0f}% 且近5日放量{recent_v/base_v:.1f}× → 情绪过热,警惕回落') if sig
            else f'未触发(乖离={bias*100:.0f}%,放量={hot})',
            'bias_pct': round(bias * 100, 1)}


def detect_regime(df: pd.DataFrame) -> str:
    """行情阶段:trending_up / trending_down / sideways / volatile(供决策/策略路由)。"""
    d = _prep(df, 25)
    if d is None:
        return 'unknown'
    c = d['close']
    ma5, ma10, ma20 = c.rolling(5).mean().iloc[-1], c.rolling(10).mean().iloc[-1], c.rolling(20).mean().iloc[-1]
    ma20_prev = c.rolling(20).mean().iloc[-6]
    slope_up = ma20 > ma20_prev
    vol = c.pct_change().iloc[-20:].std() * 100  # 近20日日收益波动率(%)
    if vol > 4:
        return 'volatile'
    if ma5 > ma10 > ma20 and slope_up:
        return 'trending_up'
    if ma5 < ma10 < ma20 and not slope_up:
        return 'trending_down'
    return 'sideways'


if __name__ == '__main__':
    import sys, os, io
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    import _bootstrap  # noqa
    from stock_data import StockDataFetcher
    df = StockDataFetcher().get_stock_data('600519', '1y')
    print('regime:', detect_regime(df))
    print('shrink_pullback:', shrink_pullback(df))
    print('bottom_volume:', bottom_volume(df))
    print('emotion_top:', emotion_top_warning(df))
