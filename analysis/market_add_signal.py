"""开盘后组合级五档买卖动作判断。

只判断总仓位动作，不推荐具体标的。五档为 strong_buy / buy / hold / reduce / sell；
保留 must_add 字段兼容旧消费者，但新代码统一读取 action。
"""

from __future__ import annotations

import math
import os
import statistics
from datetime import date
from typing import Dict, Iterable, List, Optional


CORE_INDICES = frozenset({'上证指数', '深证成指', '创业板指', '科创50', '沪深300'})

ACTION_META = {
    'strong_buy': ('🔴【今日组合动作：强力买入】', '强力买入', 2, '+10%~15%'),
    'buy': ('🔴【今日组合动作：适度买入】', '适度买入', 1, '+5%左右'),
    'hold': ('⚪【今日组合动作：持有】', '持有', 0, '不变'),
    'reduce': ('🟢【今日组合动作：适度卖出】', '适度卖出', -1, '-5%左右'),
    'sell': ('🟢【今日组合动作：优先卖出】', '优先卖出', -2, '-10%~15%'),
    'unknown': ('⚠️【今日组合动作：数据不足】', '数据不足·默认持有', 0, '不变'),
}


def _result(action: str, reason: str, **base) -> Dict:
    headline, action_cn, rank, delta = ACTION_META[action]
    return dict(base, action=action, action_cn=action_cn, action_rank=rank,
                suggested_position_delta=delta, headline=headline,
                must_add=(action == 'strong_buy'),
                allow_buy=(action in {'strong_buy', 'buy'}), reason=reason)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def evaluate(indices: Iterable[dict], previous_returns: Iterable[float],
             trend_ratio: Optional[float] = None,
             breadth: Optional[Dict] = None) -> Dict:
    """纯规则判断，便于测试。previous_returns 为沪深300最近若干完整交易日涨跌幅。"""
    changes: List[float] = []
    names: List[str] = []
    for row in indices or []:
        if row.get('name') not in CORE_INDICES:
            continue
        try:
            v = float(row.get('change_pct'))
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            changes.append(v)
            names.append(str(row.get('name')))

    if len(changes) < 3:
        return _result('unknown', f'核心指数行情仅 {len(changes)}/5 个，数据不足时保持仓位。',
                       level='unknown')

    sharp_pct = abs(_env_float('MARKET_ADD_SHARP_DROP_PCT', 1.8))
    broad_pct = abs(_env_float('MARKET_ADD_BROAD_DROP_PCT', 1.5))
    previous_pct = abs(_env_float('MARKET_ADD_PREVIOUS_DROP_PCT', 1.5))
    min_trend = _env_float('MARKET_ADD_MIN_MA20_RATIO', 0.95)
    buy_pct = abs(_env_float('MARKET_ACTION_BUY_DIP_PCT', 0.8))
    trim_pct = abs(_env_float('MARKET_ACTION_TRIM_RALLY_PCT', 1.8))

    median = statistics.median(changes)
    broad_count = sum(1 for v in changes if v <= -broad_pct)
    moderate_down = sum(1 for v in changes if v <= -buy_pct)
    broad_up = sum(1 for v in changes if v >= trim_pct)
    broad_need = max(3, math.ceil(len(changes) * 0.6))
    prev = [float(v) for v in (previous_returns or []) if v is not None and math.isfinite(float(v))]
    last_prev = prev[-1] if prev else None
    recent = prev[-3:]
    prior_large = last_prev is not None and last_prev <= -previous_pct
    cascade = prior_large or sum(1 for v in recent if v <= -1.0) >= 2
    trend_broken = trend_ratio is not None and trend_ratio < min_trend
    broad_sharp = median <= -sharp_pct and broad_count >= broad_need
    breadth = breadth if isinstance(breadth, dict) else {}
    breadth_available = bool(breadth.get('available'))
    up_ratio = breadth.get('up_ratio') if breadth_available else None
    breadth_weak = bool(up_ratio is not None and float(up_ratio) <= 0.35)
    breadth_strong = bool(up_ratio is not None and float(up_ratio) >= 0.70)
    # 有横截面时，它用于确认“普跌/普涨”；不可用时完整保持原有指数规则。
    broad_sharp = broad_sharp and (breadth_weak if breadth_available else True)

    base = {
        'median_change': round(median, 2), 'broad_down': broad_count,
        'available': len(changes), 'index_names': names,
        'previous_change': round(last_prev, 2) if last_prev is not None else None,
        'trend_ratio': round(trend_ratio, 4) if trend_ratio is not None else None,
        'breadth': breadth,
    }
    if broad_sharp and not cascade and not trend_broken:
        return _result('strong_buy',
                       (f'核心指数中位跌幅 {median:.2f}%，{broad_count}/{len(changes)} 个跌超 '
                        f'{broad_pct:.1f}%；属于非连续的全市场急跌，可明显提高总仓位。'),
                       **base, level='strong_buy')
    if broad_sharp and cascade and trend_broken:
        return _result('sell',
                       (f'核心指数中位跌幅 {median:.2f}%，近期连续下杀且沪深300明显跌破20日趋势；'
                        '系统性风险优先，先降低总仓位，不接飞刀。'),
                       **base, level='sell')
    if broad_sharp and (cascade or trend_broken):
        blocks = []
        if cascade:
            blocks.append('近期已有连续下杀')
        if trend_broken:
            blocks.append('沪深300已明显跌破20日趋势')
        return _result('reduce',
                       (f'核心指数中位跌幅 {median:.2f}%，且' + '、'.join(blocks)
                        + '；先适度降低风险仓位，等待止跌。'),
                       **base, level='reduce')
    if trend_broken:
        return _result('reduce', '沪深300明显低于20日趋势，高仓位下先小幅降风险。',
                       **base, level='reduce')
    if median >= trim_pct and broad_up >= broad_need and (breadth_strong if breadth_available else True):
        return _result('reduce',
                       (f'核心指数中位上涨 {median:.2f}%，{broad_up}/{len(changes)} 个涨超 '
                        f'{trim_pct:.1f}%；高仓位下借普涨适度兑现。'),
                       **base, level='reduce')
    if (median <= -buy_pct and moderate_down >= broad_need and not cascade
            and (breadth_weak if breadth_available else True)):
        return _result('buy',
                       (f'核心指数中位跌幅 {median:.2f}%，属于普遍回调但未形成连续下杀；'
                        '可小幅提高仓位，不需要一次打满。'),
                       **base, level='buy')
    breadth_text = ''
    if breadth_available:
        breadth_text = (f"；A500 上涨 {int(breadth.get('up_count') or 0)}/"
                        f"{int(breadth.get('covered') or 0)} 只")
    return _result('hold',
                   (f'核心指数中位涨跌 {median:+.2f}%{breadth_text}，没有达到明确买入或卖出门槛；'
                    '维持仓位，普通小波动不操作。'),
                   **base, level='hold')


def build() -> Dict:
    """拉一次实时指数 + 沪深300历史缓存并完成判断；任一异常都保守返回 unknown。"""
    try:
        import datahub
        indices = datahub.indices() or []
        previous_returns: List[float] = []
        trend_ratio = None
        df = datahub.index_kline('000300', '1mo', use_cache=True)
        if df is not None and not getattr(df, 'empty', True):
            hist = df.copy()
            try:
                hist = hist[hist.index.date < date.today()]
            except Exception:
                pass
            close_col = 'Close' if 'Close' in hist.columns else 'close'
            closes = hist[close_col].astype(float).dropna()
            previous_returns = list((closes.pct_change().dropna().tail(3) * 100).values)
            if len(closes) >= 20:
                ma20 = float(closes.tail(20).mean())
                trend_ratio = float(closes.iloc[-1]) / ma20 if ma20 > 0 else None
        try:
            from analysis.market_breadth import build as _build_breadth
            breadth = _build_breadth()
        except Exception as exc:
            breadth = {'available': False, 'reason': f'{type(exc).__name__}'}
        return evaluate(indices, previous_returns, trend_ratio, breadth=breadth)
    except Exception as e:
        return _result('unknown', f'{type(e).__name__}；数据异常时默认保持仓位。',
                       level='unknown')


def format_text(signal: Dict) -> str:
    return (f"{signal.get('headline', ACTION_META['unknown'][0])}\n"
            f"动作：{signal.get('action_cn', '数据不足·默认持有')}；"
            f"总仓位参考：{signal.get('suggested_position_delta', '不变')}。"
            f"{signal.get('reason', '')}")
