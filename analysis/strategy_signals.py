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


def rise_rollover_setup(df: pd.DataFrame, window: int = 10,
                        min_up_days: int = 7, min_return_pct: float = 15.0) -> Dict:
    """识别“阶段连续上涨”的观察底座，供下一交易日实时判断是否转弱。

    默认口径：最近 10 个交易日里至少 7 日上涨，区间累计涨幅至少 15%。
    这里只标记上涨段，不把高位本身当卖点；MA5/MA10/近3日低点等参考位会随
    快照保存，次日用 :func:`evaluate_rise_rollover` 结合实时价做分级。
    """
    d = _prep(df, window + 1)
    if d is None or len(d) < window + 1:
        return {
            'signal': False, 'setup': False, 'reason': '数据不足',
            'window': window, 'min_up_days': min_up_days,
            'min_return_pct': float(min_return_pct),
        }
    c = d['close']
    changes = c.pct_change().iloc[-window:]
    up_days = int((changes > 0).sum())
    base = float(c.iloc[-window - 1])
    last = float(c.iloc[-1])
    cumulative_pct = (last / base - 1) * 100 if base else 0.0
    setup = bool(up_days >= min_up_days and cumulative_pct >= min_return_pct)

    ma5 = float(c.iloc[-5:].mean())
    ma10 = float(c.iloc[-10:].mean())
    low3 = float(d['low'].iloc[-3:].min())
    peak10 = float(d['high'].iloc[-window:].max())
    avg_volume5 = float(d['volume'].iloc[-5:].mean())
    reason = (f'{window}日{up_days}涨、累计+{cumulative_pct:.1f}%，进入转弱观察'
              if setup else
              f'未满足({window}日{up_days}涨、累计{cumulative_pct:+.1f}%)')
    return {
        'signal': setup, 'setup': setup, 'reason': reason,
        'window': int(window), 'min_up_days': int(min_up_days),
        'min_return_pct': float(min_return_pct), 'up_days': up_days,
        'cumulative_pct': round(cumulative_pct, 2),
        'reference_close': round(last, 4), 'ma5': round(ma5, 4),
        'ma10': round(ma10, 4), 'low3': round(low3, 4),
        'peak10': round(peak10, 4), 'avg_volume5': round(avg_volume5, 2),
    }


def evaluate_rise_rollover(setup: Dict, current_price: float,
                           change_pct: float, volume_ratio: float = 0.0) -> Dict:
    """用实时价评估“连续上涨后开始下跌”，返回 0~3 级信号。

    0=未转弱；1=首次转弱、只观察；2=转弱确认、评估锁利；3=趋势破位、减仓防守。
    阈值有意留出噪声区：普通小跌不触发，且 1 级不会增加卖出风险分。
    """
    base = {
        'signal': False, 'warning': False, 'level': 0, 'level_name': '未转弱',
        'action': 'hold', 'risk_score': 0, 'reason': '未形成阶段连涨',
    }
    if not isinstance(setup, dict) or not setup.get('setup', setup.get('signal', False)):
        return base
    try:
        price = float(current_price or 0)
        change = float(change_pct or 0)
        vr = float(volume_ratio or 0)
        ma5 = float(setup.get('ma5') or 0)
        ma10 = float(setup.get('ma10') or 0)
        low3 = float(setup.get('low3') or 0)
    except (TypeError, ValueError):
        return {**base, 'reason': '实时行情数据不足'}
    if price <= 0:
        return {**base, 'reason': '实时价格缺失'}

    # 0.5% 缓冲避免刚好贴着均线/前低时被浮点或盘中噪声误报。
    break_ma5 = bool(ma5 > 0 and price < ma5 * 0.995)
    break_ma10 = bool(ma10 > 0 and price < ma10 * 0.995)
    break_low3 = bool(low3 > 0 and price < low3 * 0.995)
    mild_drop = change <= -1.8
    hard_drop = change <= -3.0
    volume_confirm = bool(change < 0 and vr >= 1.3)

    level = 0
    if mild_drop or break_ma5:
        level = 1
    if break_low3 or hard_drop or (mild_drop and (break_ma5 or volume_confirm)):
        level = 2
    if break_ma10 or (hard_drop and volume_confirm) or (break_low3 and (mild_drop or volume_confirm)):
        level = 3

    if level == 0:
        return {
            **base, 'reason': (f"{setup.get('window', 10)}日{setup.get('up_days', 0)}涨、"
                                f"累计+{float(setup.get('cumulative_pct') or 0):.1f}%，但尚未转弱"),
            'change_pct': round(change, 2), 'volume_ratio': round(vr, 2),
        }

    facts = []
    if hard_drop:
        facts.append(f'当日跌{abs(change):.1f}%')
    elif mild_drop:
        facts.append(f'当日跌{abs(change):.1f}%')
    if break_ma10:
        facts.append('跌破10日线')
    elif break_ma5:
        facts.append('跌破5日线')
    if break_low3:
        facts.append('跌破近3日低点')
    if volume_confirm:
        facts.append(f'量比{vr:.1f}')
    prefix = (f"{setup.get('window', 10)}日{setup.get('up_days', 0)}涨/"
              f"累计+{float(setup.get('cumulative_pct') or 0):.1f}%后")
    meta = {
        1: ('首次转弱', 'watch', 0, '先观察，不因普通小跌卖出'),
        2: ('转弱确认', 'protect_profit', 1, '评估锁利，等确认再减'),
        3: ('趋势破位', 'reduce', 2, '减仓防守，保护已有利润'),
    }[level]
    return {
        'signal': level >= 2, 'warning': True, 'level': level,
        'level_name': meta[0], 'action': meta[1], 'risk_score': meta[2],
        'reason': f"{prefix}{'、'.join(facts)}；{meta[3]}",
        'change_pct': round(change, 2), 'volume_ratio': round(vr, 2),
        'break_ma5': break_ma5, 'break_ma10': break_ma10,
        'break_low3': break_low3, 'volume_confirm': volume_confirm,
        'up_days': int(setup.get('up_days') or 0),
        'cumulative_pct': round(float(setup.get('cumulative_pct') or 0), 2),
    }


def rise_rollover_warning(df: pd.DataFrame) -> Dict:
    """对日 K 直接判断最后一日是否出现“10日7涨且累计15%后的转弱”。"""
    d = _prep(df, 12)
    if d is None or len(d) < 12:
        return {'signal': False, 'warning': False, 'level': 0, 'reason': '数据不足'}
    history = d.iloc[:-1].reset_index(drop=True)
    setup = rise_rollover_setup(history)
    prev_close = float(history['close'].iloc[-1])
    price = float(d['close'].iloc[-1])
    change = (price / prev_close - 1) * 100 if prev_close else 0.0
    avg_volume = float(history['volume'].iloc[-5:].mean())
    vr = float(d['volume'].iloc[-1]) / avg_volume if avg_volume else 0.0
    return evaluate_rise_rollover(setup, price, change, vr)


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
    print('rise_rollover:', rise_rollover_warning(df))
