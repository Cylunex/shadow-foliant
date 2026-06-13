"""
风险度量与压力测试 — 借鉴 SkillHub「风险分析与压力测试」skill

提供（纯 numpy/pandas，无 scipy 依赖）：
  - 历史/参数法 VaR、CVaR(条件风险价值/ES)
  - 最大回撤 MaxDrawdown
  - 年化波动率、年化收益、夏普、下行波动/索提诺
  - 蒙特卡洛 GBM 模拟未来区间收益分位
  - 情景压力测试（自定义冲击 + 内置极端情景）

定位：补强本项目风险模块（原仅阈值/ATR），给风险管理师 Agent 提供量化风险上下文。
输入：股价或收益序列；可直接吃 StockDataFetcher.get_stock_data() 的 K线 DataFrame。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any
import numpy as np
import pandas as pd

TRADING_DAYS = 252
# 正态分位（避免 scipy 依赖，硬编码常用置信度）
_Z = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263}


# =============================================================================
# 基础：从价格/DataFrame 提取日收益
# =============================================================================
def to_returns(data) -> pd.Series:
    """从价格序列 / 含 Close 列的 DataFrame 计算日简单收益率。"""
    if hasattr(data, 'columns'):
        cols = {str(c).lower(): c for c in data.columns}
        if 'close' in cols:
            px = pd.to_numeric(data[cols['close']], errors='coerce')
        else:
            raise ValueError('DataFrame 缺少 Close 列')
    else:
        px = pd.Series(pd.to_numeric(pd.Series(data), errors='coerce'))
    return px.pct_change().dropna()


# =============================================================================
# VaR / CVaR
# =============================================================================
def var_historical(returns: pd.Series, conf: float = 0.95) -> float:
    """历史模拟法 VaR（返回正数=潜在损失比例，如 0.03=3%）。"""
    r = pd.to_numeric(returns, errors='coerce').dropna()
    if len(r) < 20:
        return float('nan')
    q = r.quantile(1 - conf)
    return float(-q)


def cvar_historical(returns: pd.Series, conf: float = 0.95) -> float:
    """历史 CVaR / ES：超过 VaR 的尾部平均损失。"""
    r = pd.to_numeric(returns, errors='coerce').dropna()
    if len(r) < 20:
        return float('nan')
    thr = r.quantile(1 - conf)
    tail = r[r <= thr]
    return float(-tail.mean()) if len(tail) else float('nan')


def var_parametric(returns: pd.Series, conf: float = 0.95) -> float:
    """参数法（正态）VaR = -(μ - z·σ)。"""
    r = pd.to_numeric(returns, errors='coerce').dropna()
    if len(r) < 20:
        return float('nan')
    z = _Z.get(conf, 1.6449)
    return float(-(r.mean() - z * r.std(ddof=0)))


# =============================================================================
# 回撤 / 风险调整收益
# =============================================================================
def max_drawdown(data) -> Dict[str, Any]:
    """最大回撤（接受价格序列或含 Close 的 DataFrame）。"""
    if hasattr(data, 'columns'):
        cols = {str(c).lower(): c for c in data.columns}
        px = pd.to_numeric(data[cols['close']], errors='coerce').dropna()
    else:
        px = pd.to_numeric(pd.Series(data), errors='coerce').dropna()
    if len(px) < 2:
        return {'max_drawdown': float('nan')}
    cummax = px.cummax()
    dd = px / cummax - 1.0
    trough = dd.idxmin()
    return {
        'max_drawdown': float(dd.min()),          # 负数，如 -0.32 = -32%
        'trough_date': str(trough),
        'recovered': bool(px.iloc[-1] >= cummax.loc[trough]),
    }


def risk_adjusted(returns: pd.Series, rf: float = 0.0) -> Dict[str, float]:
    """年化收益/波动 + 夏普 + 索提诺（下行波动）。"""
    r = pd.to_numeric(returns, errors='coerce').dropna()
    if len(r) < 20:
        return {}
    ann_ret = float(r.mean() * TRADING_DAYS)
    ann_vol = float(r.std(ddof=0) * np.sqrt(TRADING_DAYS))
    downside = r[r < 0]
    dd_vol = float(downside.std(ddof=0) * np.sqrt(TRADING_DAYS)) if len(downside) else float('nan')
    sharpe = (ann_ret - rf) / ann_vol if ann_vol else float('nan')
    sortino = (ann_ret - rf) / dd_vol if dd_vol and not np.isnan(dd_vol) and dd_vol != 0 else float('nan')
    return {
        'annual_return': round(ann_ret, 4),
        'annual_vol': round(ann_vol, 4),
        'sharpe': round(sharpe, 3),
        'sortino': round(sortino, 3) if not np.isnan(sortino) else float('nan'),
    }


# =============================================================================
# 蒙特卡洛 GBM
# =============================================================================
def monte_carlo_gbm(returns: pd.Series, horizon: int = 20, n_sims: int = 5000,
                    conf: float = 0.95, seed: int = 7) -> Dict[str, Any]:
    """几何布朗运动蒙特卡洛，模拟 horizon 个交易日后的累计收益分布。"""
    r = pd.to_numeric(returns, errors='coerce').dropna()
    if len(r) < 20:
        return {}
    mu, sigma = float(r.mean()), float(r.std(ddof=0))
    rng = np.random.RandomState(seed)
    # 每条路径累计对数收益 ~ sum of daily (mu-0.5σ²)+σZ
    drift = (mu - 0.5 * sigma ** 2) * horizon
    shock = sigma * np.sqrt(horizon) * rng.standard_normal(n_sims)
    horizon_ret = np.exp(drift + shock) - 1.0
    return {
        'horizon_days': horizon,
        'n_sims': n_sims,
        'expected_return': round(float(np.mean(horizon_ret)), 4),
        'var': round(float(-np.quantile(horizon_ret, 1 - conf)), 4),
        'cvar': round(float(-horizon_ret[horizon_ret <= np.quantile(horizon_ret, 1 - conf)].mean()), 4),
        'p05': round(float(np.quantile(horizon_ret, 0.05)), 4),
        'p50': round(float(np.quantile(horizon_ret, 0.50)), 4),
        'p95': round(float(np.quantile(horizon_ret, 0.95)), 4),
        'prob_loss': round(float(np.mean(horizon_ret < 0)), 4),
    }


# =============================================================================
# 情景压力测试
# =============================================================================
DEFAULT_SCENARIOS = {
    '单日急跌-5%': -0.05,
    '单日跌停-10%': -0.10,
    '连续回调-15%': -0.15,
    '系统性风险-20%': -0.20,
    '极端股灾-30%': -0.30,
}


def stress_test(current_price: float, position_value: float = None,
                shocks: Dict[str, float] = None, beta: float = 1.0) -> List[Dict[str, Any]]:
    """情景压力测试：对当前价/持仓市值施加一组冲击（按 beta 放大市场冲击）。"""
    shocks = shocks or DEFAULT_SCENARIOS
    out = []
    for name, mkt_shock in shocks.items():
        stock_shock = mkt_shock * (beta if beta else 1.0)
        row = {
            'scenario': name,
            'market_shock': mkt_shock,
            'stock_shock': round(stock_shock, 4),
            'price_after': round(current_price * (1 + stock_shock), 3) if current_price else None,
        }
        if position_value:
            row['pnl'] = round(position_value * stock_shock, 2)
            row['value_after'] = round(position_value * (1 + stock_shock), 2)
        out.append(row)
    return out


# =============================================================================
# 主入口 + 摘要
# =============================================================================
def analyze_risk(data, current_price: float = None, position_value: float = None,
                 beta: float = 1.0, conf: float = 0.95) -> Dict[str, Any]:
    """对单标的做完整量化风险分析。data 可为 K线 DataFrame 或价格序列。"""
    out: Dict[str, Any] = {'available': False, 'errors': []}
    try:
        rets = to_returns(data)
        if len(rets) < 20:
            out['errors'].append(f'收益样本不足({len(rets)}<20)')
            return out
        if current_price is None and hasattr(data, 'columns'):
            cols = {str(c).lower(): c for c in data.columns}
            if 'close' in cols:
                current_price = float(pd.to_numeric(data[cols['close']], errors='coerce').dropna().iloc[-1])
        out.update({
            'available': True,
            'conf': conf,
            'sample_days': int(len(rets)),
            'var_hist': round(var_historical(rets, conf), 4),
            'cvar_hist': round(cvar_historical(rets, conf), 4),
            'var_param': round(var_parametric(rets, conf), 4),
            'max_drawdown': max_drawdown(data),
            'risk_adjusted': risk_adjusted(rets),
            'monte_carlo': monte_carlo_gbm(rets, horizon=20, conf=conf),
            'stress': stress_test(current_price or 0, position_value, beta=beta),
        })
        out['summary'] = format_risk_summary(out)
    except Exception as e:
        out['errors'].append(f'风险计算异常: {e}')
    return out


def format_risk_summary(r: Dict[str, Any]) -> str:
    """量化风险中文摘要，供风险管理师 Agent prompt 注入。"""
    if not r.get('available'):
        return '量化风险：数据不足或不可用。'
    conf_pct = int(r['conf'] * 100)
    ra = r.get('risk_adjusted', {})
    mc = r.get('monte_carlo', {})
    md = r.get('max_drawdown', {})
    parts = [
        f"【量化风险】样本{r['sample_days']}日。",
        f"VaR({conf_pct}%) 历史法 {r['var_hist']*100:.2f}% / 参数法 {r['var_param']*100:.2f}%（单日潜在最大亏损），"
        f"CVaR(尾部均亏) {r['cvar_hist']*100:.2f}%。",
        f"历史最大回撤 {md.get('max_drawdown', float('nan'))*100:.1f}%。",
    ]
    if ra:
        parts.append(f"年化波动 {ra.get('annual_vol',0)*100:.1f}%，夏普 {ra.get('sharpe')}，索提诺 {ra.get('sortino')}。")
    if mc:
        parts.append(f"蒙特卡洛(未来20日)：预期 {mc.get('expected_return',0)*100:.1f}%，"
                     f"亏损概率 {mc.get('prob_loss',0)*100:.0f}%，5分位 {mc.get('p05',0)*100:.1f}%。")
    stress = r.get('stress') or []
    big = next((s for s in stress if s['scenario'].startswith('系统性')), None)
    if big and big.get('price_after') is not None:
        parts.append(f"压力情景「{big['scenario']}」→ 价格约 {big['price_after']}（含 beta）。")
    return ' '.join(parts)


if __name__ == '__main__':
    # 自测：合成一年价格序列（带波动与一段回撤）
    rng = np.random.RandomState(1)
    daily = rng.normal(0.0005, 0.02, 250)
    daily[120:140] -= 0.01  # 制造一段回撤
    price = 100 * np.cumprod(1 + daily)
    df = pd.DataFrame({'Close': price}, index=pd.date_range('2025-01-01', periods=250, freq='D'))
    res = analyze_risk(df, beta=1.2, position_value=100000)
    import json
    print(json.dumps({k: v for k, v in res.items() if k not in ('stress',)}, ensure_ascii=False, indent=2, default=str))
    print("\nSTRESS:")
    for s in res['stress']:
        print(" ", s)
    print("\nSUMMARY:", res['summary'])
