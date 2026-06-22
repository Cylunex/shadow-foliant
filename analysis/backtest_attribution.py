import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""组合回测·分层归因 —— 借鉴 Vibe-Trading 的 post-backtest attribution。

回答"这条回测净值到底是真本事还是运气":对 portfolio_backtest 的结果做 4 层确定性归因
(纯 numpy,无 scipy/新依赖;p 值用正态近似,n>30 足够准),让"赚了多少"之外还能看清
**靠什么赚、是否经得起统计检验**:

  Layer 1 交易归因   top 盈亏单 · 按离场原因/持有周期分桶 · 去掉 top5 盈利单是否仍赚(鲁棒性)
  Layer 2 β 回归     策略日收益 vs 基准(沪深300)OLS → 年化α/β/R²/α的t值;|t(α)|<2 警示"超额不显著"
  Layer 3 市况归因   按基准 regime(牛/熊/震荡)给交易分桶;>60% 盈利集中单一市况则警示"挑市"
  Layer 4 蒙特卡洛   MaxDD 顺序置换检验(回撤是路径依赖的)+ Sharpe 显著性(t=Sharpe·√N)

接口:
  attribute(bt_result) -> dict     # bt_result = portfolio_backtest(...) 的返回
  format_attribution(attr) -> str  # 文本报告(推送/LLM 复核用)
"""

import math
from typing import Any, Dict, List, Optional

import numpy as np

TRADING_DAYS = 252
_SEED = 42  # 蒙特卡洛置换固定种子 → 结果可复现


def _norm_cdf(x: float) -> float:
    """标准正态 CDF(erf,无需 scipy)。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _two_sided_p(t: float) -> float:
    """由 t 值取双尾 p(正态近似)。"""
    return round(2.0 * (1.0 - _norm_cdf(abs(t))), 4)


def _max_drawdown_from_returns(rets: np.ndarray) -> float:
    """由日收益序列算最大回撤(%,负值)。"""
    if len(rets) == 0:
        return 0.0
    equity = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(equity)
    return float(((equity - peak) / peak).min() * 100.0)


# ── Layer 1:交易归因 ──────────────────────────────────────────────
def _trade_attribution(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {'n_trades': 0}
    pnls = [float(t.get('pnl') or 0) for t in trades]
    total_pnl = round(sum(pnls), 2)

    def _slim(t):
        return {'symbol': t.get('symbol'), 'name': t.get('name', ''),
                'exit_date': t.get('exit_date'), 'ret_pct': t.get('ret_pct'),
                'pnl': round(float(t.get('pnl') or 0), 2),
                'hold_bars': t.get('hold_bars'), 'exit_reason': t.get('exit_reason')}

    srt = sorted(trades, key=lambda t: float(t.get('pnl') or 0), reverse=True)
    top_winners = [_slim(t) for t in srt[:5] if float(t.get('pnl') or 0) > 0]
    top_losers = [_slim(t) for t in srt[::-1][:5] if float(t.get('pnl') or 0) < 0]

    # 离场原因分桶
    by_reason: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        b = by_reason.setdefault(t.get('exit_reason') or 'unknown',
                                 {'count': 0, 'pnl': 0.0, 'wins': 0})
        b['count'] += 1
        b['pnl'] += float(t.get('pnl') or 0)
        b['wins'] += 1 if float(t.get('pnl') or 0) > 0 else 0
    reason_rows = sorted(
        [{'reason': k, 'count': v['count'], 'total_pnl': round(v['pnl'], 2),
          'avg_pnl': round(v['pnl'] / v['count'], 2),
          'win_rate_pct': round(v['wins'] / v['count'] * 100, 1)}
         for k, v in by_reason.items()],
        key=lambda x: -x['total_pnl'])

    # 持有周期分桶(按 bar 数:短≤3 / 中4-20 / 长>20)
    buckets = {'短(≤3bar)': [], '中(4-20bar)': [], '长(>20bar)': []}
    for t in trades:
        hb = t.get('hold_bars') or 0
        key = '短(≤3bar)' if hb <= 3 else ('中(4-20bar)' if hb <= 20 else '长(>20bar)')
        buckets[key].append(t)
    hold_rows = []
    for k, ts in buckets.items():
        if not ts:
            continue
        ps = [float(x.get('pnl') or 0) for x in ts]
        rs = [float(x.get('ret_pct') or 0) for x in ts]
        hold_rows.append({
            'bucket': k, 'count': len(ts), 'total_pnl': round(sum(ps), 2),
            'win_rate_pct': round(sum(1 for p in ps if p > 0) / len(ts) * 100, 1),
            'avg_ret_pct': round(float(np.mean(rs)), 2)})

    # 鲁棒性:剔除 top5 盈利单后是否仍盈利(避免少数票撑起全部收益)
    top5_win_pnl = sum(float(t.get('pnl') or 0) for t in srt[:5] if float(t.get('pnl') or 0) > 0)
    pnl_ex_top5 = round(total_pnl - top5_win_pnl, 2)
    return {
        'n_trades': len(trades),
        'total_pnl': total_pnl,
        'top_winners': top_winners,
        'top_losers': top_losers,
        'by_exit_reason': reason_rows,
        'by_holding': hold_rows,
        'robustness': {
            'total_pnl': total_pnl,
            'pnl_ex_top5_winners': pnl_ex_top5,
            'profitable_ex_top5': pnl_ex_top5 > 0,
            'top5_pnl_share_pct': round(top5_win_pnl / total_pnl * 100, 1) if total_pnl > 0 else None,
        },
    }


# ── 从 equity_curve 取策略/基准对齐日收益 ──────────────────────────
def _aligned_returns(equity_curve: List[Dict[str, Any]]):
    navs, bnavs = [], []
    for pt in equity_curve:
        nv, bn = pt.get('nav'), pt.get('bench_nav')
        if nv is None:
            continue
        navs.append(float(nv))
        bnavs.append(float(bn) if bn is not None else np.nan)
    nav = np.array(navs, dtype='float64')
    bnav = np.array(bnavs, dtype='float64')
    s_ret = np.diff(nav) / nav[:-1] if len(nav) > 1 else np.array([])
    if len(bnav) > 1:
        b_ret = np.diff(bnav) / bnav[:-1]
    else:
        b_ret = np.array([])
    return s_ret, b_ret


# ── Layer 2:β 回归(策略 vs 基准)───────────────────────────────────
def _beta_regression(s_ret: np.ndarray, b_ret: np.ndarray) -> Optional[Dict[str, Any]]:
    if len(s_ret) < 30 or len(b_ret) != len(s_ret):
        return None
    mask = ~np.isnan(b_ret) & ~np.isnan(s_ret)
    x, y = b_ret[mask], s_ret[mask]
    n = len(x)
    if n < 30 or np.std(x) == 0:
        return None
    vb = np.var(x, ddof=1)
    beta = float(np.cov(y, x, ddof=1)[0, 1] / vb)
    alpha = float(np.mean(y) - beta * np.mean(x))           # 日均 α
    resid = y - (alpha + beta * x)
    s_resid = float(np.std(resid, ddof=2)) if n > 2 else 0.0
    mean_x = float(np.mean(x))
    se_alpha = s_resid * math.sqrt(1.0 / n + mean_x ** 2 / (n * vb)) if s_resid > 0 else 0.0
    t_alpha = alpha / se_alpha if se_alpha > 0 else 0.0
    # R²
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    ss_res = float(np.sum(resid ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {
        'n_days': n,
        'alpha_annual_pct': round(alpha * TRADING_DAYS * 100, 2),  # 年化 α
        'beta': round(beta, 3),
        'r_squared': round(r2, 3),
        't_alpha': round(t_alpha, 2),
        'p_alpha': _two_sided_p(t_alpha),
        'alpha_significant': abs(t_alpha) >= 2.0,
        'note': ('超额收益统计显著(|t|≥2),不像运气' if abs(t_alpha) >= 2.0
                 else '⚠️ 超额收益与基准无统计差异(|t|<2),α 可能只是噪声'),
    }


# ── Layer 3:市况(regime)归因 ─────────────────────────────────────
def _regime_attribution(equity_curve: List[Dict[str, Any]],
                        trades: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # 用基准 20 日滚动累计涨跌给每个交易日定市况:>+3% 牛 / <-3% 熊 / 其余 震荡
    dates, bnav = [], []
    for pt in equity_curve:
        if pt.get('bench_nav') is not None:
            dates.append(pt['date'])
            bnav.append(float(pt['bench_nav']))
    if len(bnav) < 25 or not trades:
        return None
    bnav = np.array(bnav)
    regime_by_date: Dict[str, str] = {}
    W = 20
    for i, d in enumerate(dates):
        if i < W:
            regime_by_date[d] = '震荡'
            continue
        chg = bnav[i] / bnav[i - W] - 1.0
        regime_by_date[d] = '牛市' if chg > 0.03 else ('熊市' if chg < -0.03 else '震荡')
    agg: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        reg = regime_by_date.get(t.get('entry_date'), '震荡')
        b = agg.setdefault(reg, {'count': 0, 'pnl': 0.0, 'wins': 0})
        b['count'] += 1
        b['pnl'] += float(t.get('pnl') or 0)
        b['wins'] += 1 if float(t.get('pnl') or 0) > 0 else 0
    total_pos = sum(v['pnl'] for v in agg.values() if v['pnl'] > 0) or 1e-9
    rows, flag = [], False
    for reg in ('牛市', '震荡', '熊市'):
        v = agg.get(reg)
        if not v:
            continue
        share = v['pnl'] / total_pos * 100 if v['pnl'] > 0 else 0
        if share > 60:
            flag = True
        rows.append({'regime': reg, 'trades': v['count'],
                     'win_rate_pct': round(v['wins'] / v['count'] * 100, 1),
                     'total_pnl': round(v['pnl'], 2),
                     'pos_pnl_share_pct': round(share, 1)})
    return {'by_regime': rows, 'concentration_flag': flag,
            'note': ('⚠️ 超 60% 盈利集中在单一市况,策略可能只在特定行情有效'
                     if flag else '盈利在不同市况间较分散')}


# ── Layer 4:蒙特卡洛 ──────────────────────────────────────────────
def _monte_carlo(s_ret: np.ndarray, n_shuffles: int = 2000) -> Optional[Dict[str, Any]]:
    if len(s_ret) < 30:
        return None
    actual_dd = _max_drawdown_from_returns(s_ret)
    rng = np.random.RandomState(_SEED)
    worse = 0
    for _ in range(n_shuffles):
        perm = rng.permutation(s_ret)         # 置换顺序(均值/方差不变,但回撤路径变)
        if _max_drawdown_from_returns(perm) <= actual_dd:   # 随机排列回撤更深(更差或相等)
            worse += 1
    dd_p = round(worse / n_shuffles, 4)
    # Sharpe 显著性:t = sharpe_daily · √N(顺序无关,故单独算)
    mu, sd = float(np.mean(s_ret)), float(np.std(s_ret, ddof=1))
    sharpe_daily = mu / sd if sd > 0 else 0.0
    t_sharpe = sharpe_daily * math.sqrt(len(s_ret))
    return {
        'n_shuffles': n_shuffles,
        'maxdd_actual_pct': round(actual_dd, 2),
        'maxdd_permutation_p': dd_p,
        'maxdd_note': ('实际回撤路径并不比随机排列更糟(p>0.05),回撤深度大体是顺序运气'
                       if dd_p > 0.05 else '实际回撤显著深于随机排列(p≤0.05),存在结构性回撤风险'),
        'sharpe_annual': round(sharpe_daily * math.sqrt(TRADING_DAYS), 2),
        'sharpe_t_stat': round(t_sharpe, 2),
        'sharpe_p': _two_sided_p(t_sharpe),
        'sharpe_significant': abs(t_sharpe) >= 2.0,
        'sharpe_note': ('收益率显著偏正(|t|≥2),不像随机' if abs(t_sharpe) >= 2.0
                        else '⚠️ 收益率与 0 无统计差异(|t|<2),业绩可能不可区分于随机'),
    }


def attribute(bt_result: Dict[str, Any], mc_shuffles: int = 2000) -> Dict[str, Any]:
    """对 portfolio_backtest 的返回做分层归因。bt_result 须含 trades / equity_curve。"""
    if not isinstance(bt_result, dict) or bt_result.get('error'):
        return {'ok': False, 'error': (bt_result or {}).get('error', 'invalid_result')}
    trades = bt_result.get('trades') or []
    curve = bt_result.get('equity_curve') or []
    s_ret, b_ret = _aligned_returns(curve)
    attr = {
        'ok': True,
        'trade_attribution': _trade_attribution(trades),
        'beta_regression': _beta_regression(s_ret, b_ret),
        'regime_attribution': _regime_attribution(curve, trades),
        'monte_carlo': _monte_carlo(s_ret, n_shuffles=mc_shuffles),
    }
    attr['verdict'] = _verdict(attr)
    return attr


def _verdict(attr: Dict[str, Any]) -> str:
    """一句话裁决:综合 α 显著性 + Sharpe 显著性 + 鲁棒性 + 市况集中度。"""
    flags = []
    br, mc = attr.get('beta_regression'), attr.get('monte_carlo')
    rob = (attr.get('trade_attribution') or {}).get('robustness') or {}
    reg = attr.get('regime_attribution') or {}
    if mc and not mc.get('sharpe_significant'):
        flags.append('收益不显著')
    if br and not br.get('alpha_significant'):
        flags.append('α不显著')
    if rob and rob.get('profitable_ex_top5') is False:
        flags.append('盈利靠少数票')
    if reg.get('concentration_flag'):
        flags.append('挑市况')
    if not flags:
        return '✅ 业绩较稳健:收益/超额统计显著,且不依赖个别票或单一市况'
    return '⚠️ 需警惕:' + '、'.join(flags) + '(结论谨慎对待)'


def format_attribution(attr: Dict[str, Any]) -> str:
    if not attr.get('ok'):
        return f"[归因失败] {attr.get('error')}"
    L = ['=== 组合回测·分层归因 ===', attr['verdict']]
    ta = attr.get('trade_attribution') or {}
    if ta.get('n_trades'):
        L.append(f"\n[交易归因]  成交 {ta['n_trades']} 笔  总盈亏 {ta['total_pnl']:+.0f}")
        rob = ta.get('robustness') or {}
        if rob.get('top5_pnl_share_pct') is not None:
            L.append(f"  Top5 盈利单占总盈利 {rob['top5_pnl_share_pct']}%;"
                     f"剔除后 {'仍盈利' if rob['profitable_ex_top5'] else '转亏'}"
                     f"({rob['pnl_ex_top5_winners']:+.0f})")
        for r in ta.get('by_exit_reason', []):
            L.append(f"  离场[{r['reason']:>8s}] {r['count']}笔 胜率{r['win_rate_pct']}% 盈亏{r['total_pnl']:+.0f}")
        for r in ta.get('by_holding', []):
            L.append(f"  持有[{r['bucket']}] {r['count']}笔 胜率{r['win_rate_pct']}% 均收益{r['avg_ret_pct']:+}%")
    br = attr.get('beta_regression')
    if br:
        L.append(f"\n[β回归 vs 基准]  年化α {br['alpha_annual_pct']:+}%  β {br['beta']}  "
                 f"R² {br['r_squared']}  t(α) {br['t_alpha']}")
        L.append('  ' + br['note'])
    reg = attr.get('regime_attribution')
    if reg:
        L.append('\n[市况归因]')
        for r in reg.get('by_regime', []):
            L.append(f"  {r['regime']}: {r['trades']}笔 胜率{r['win_rate_pct']}% "
                     f"盈亏{r['total_pnl']:+.0f} 占正盈利{r['pos_pnl_share_pct']}%")
        L.append('  ' + reg['note'])
    mc = attr.get('monte_carlo')
    if mc:
        L.append(f"\n[蒙特卡洛 {mc['n_shuffles']}次]  实际MaxDD {mc['maxdd_actual_pct']}% "
                 f"(置换p={mc['maxdd_permutation_p']})  年化Sharpe {mc['sharpe_annual']} "
                 f"(t={mc['sharpe_t_stat']}, p={mc['sharpe_p']})")
        L.append('  ' + mc['sharpe_note'])
        L.append('  ' + mc['maxdd_note'])
    return '\n'.join(L)


if __name__ == '__main__':
    import io
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 回测归因自检 ===')
    import datahub
    from portfolio_backtest import portfolio_backtest
    pool = [('600519', '茅台'), ('000858', '五粮液'), ('600036', '招商银行'),
            ('000333', '美的集团'), ('600276', '恒瑞医药'), ('002415', '海康威视')]
    r = portfolio_backtest(pool, start='2024-01-01', end='2025-12-31',
                           strategy_id='enter', hold_days=10, stop_pct=8.0, target_pct=15.0,
                           max_positions=3, initial_cash=1_000_000,
                           df_fetcher=lambda c, p: datahub.kline(c, p))
    if r.get('error'):
        print('回测失败:', r)
    else:
        print(format_attribution(attribute(r, mc_shuffles=500)))
