"""开盘后组合级“是否必须加仓”判断。

只判断市场窗口，不推荐具体标的。规则刻意保守：小跌忽略；连续下杀不接飞刀；只有核心指数
同步明显急跌、且前几日并非连续大跌、长期趋势尚未严重破坏时，才给出醒目的必须加仓提示。
"""

from __future__ import annotations

import math
import os
import statistics
from datetime import date
from typing import Dict, Iterable, List, Optional


CORE_INDICES = frozenset({'上证指数', '深证成指', '创业板指', '科创50', '沪深300'})


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def evaluate(indices: Iterable[dict], previous_returns: Iterable[float],
             trend_ratio: Optional[float] = None) -> Dict:
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
        return {
            'level': 'unknown', 'must_add': False,
            'headline': '⚠️【加仓判断不可用】',
            'reason': f'核心指数行情仅 {len(changes)}/5 个，数据不足时不触发加仓。',
        }

    sharp_pct = abs(_env_float('MARKET_ADD_SHARP_DROP_PCT', 1.8))
    broad_pct = abs(_env_float('MARKET_ADD_BROAD_DROP_PCT', 1.5))
    previous_pct = abs(_env_float('MARKET_ADD_PREVIOUS_DROP_PCT', 1.5))
    min_trend = _env_float('MARKET_ADD_MIN_MA20_RATIO', 0.95)

    median = statistics.median(changes)
    broad_count = sum(1 for v in changes if v <= -broad_pct)
    broad_need = max(3, math.ceil(len(changes) * 0.6))
    prev = [float(v) for v in (previous_returns or []) if v is not None and math.isfinite(float(v))]
    last_prev = prev[-1] if prev else None
    recent = prev[-3:]
    prior_large = last_prev is not None and last_prev <= -previous_pct
    cascade = prior_large or sum(1 for v in recent if v <= -1.0) >= 2
    trend_broken = trend_ratio is not None and trend_ratio < min_trend
    broad_sharp = median <= -sharp_pct and broad_count >= broad_need

    base = {
        'median_change': round(median, 2), 'broad_down': broad_count,
        'available': len(changes), 'index_names': names,
        'previous_change': round(last_prev, 2) if last_prev is not None else None,
        'trend_ratio': round(trend_ratio, 4) if trend_ratio is not None else None,
    }
    if broad_sharp and not cascade and not trend_broken:
        return dict(base, level='must_add', must_add=True,
                    headline='🚨🚨【今天出现必须加仓窗口】',
                    reason=(f'核心指数中位跌幅 {median:.2f}%，{broad_count}/{len(changes)} 个跌超 '
                            f'{broad_pct:.1f}%；属于非连续的全市场急跌。只提高总仓位，不指定标的。'))
    if broad_sharp and (cascade or trend_broken):
        blocks = []
        if cascade:
            blocks.append('近期已有连续下杀')
        if trend_broken:
            blocks.append('沪深300已明显跌破20日趋势')
        return dict(base, level='hold_fire', must_add=False,
                    headline='🛑【今天不要急着加仓】',
                    reason=(f'虽然核心指数中位跌幅 {median:.2f}%，但' + '、'.join(blocks)
                            + '，先防接飞刀，等待止跌。'))
    return dict(base, level='no_add', must_add=False,
                headline='🧱【今天无需加仓】',
                reason=(f'核心指数中位涨跌 {median:+.2f}%，未达到“非连续全市场急跌”门槛；'
                        '普通小跌按原计划忽略。'))


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
        return evaluate(indices, previous_returns, trend_ratio)
    except Exception as e:
        return {
            'level': 'unknown', 'must_add': False,
            'headline': '⚠️【加仓判断不可用】',
            'reason': f'{type(e).__name__}；数据异常时默认不加仓。',
        }


def format_text(signal: Dict) -> str:
    return f"{signal.get('headline', '⚠️【加仓判断不可用】')}\n结论：{'是' if signal.get('must_add') else '否'}。{signal.get('reason', '')}"
