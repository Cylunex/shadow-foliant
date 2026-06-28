"""
多因子横截面选股引擎 — 借鉴 SkillHub「多因子选股策略 / 因子研究框架」

核心链路（与数据源解耦，可离线测试）：
    因子矩阵(股票×因子) → 去极值(winsorize) → 横截面 Z-score 标准化(含方向)
    → 加权合成 → TopN 排名
另含因子有效性检验（IC / IR，借鉴 factor-research）。

设计取舍：
  - **引擎纯计算**（pandas/numpy），不绑定任何取数方式，便于单测与复用；
  - **可选 loader** 默认用「指数成分股」(沪深300/中证500) 作股票池——范围可控、
    比全市场拉数稳，复用 fundamental_scoring.collect_factors 取因子；
  - 因子方向沿用本项目 fundamental_scoring 口径（forward_pe/peg/pb/debt_ratio 越低越好，
    roe/净利增速/股息率/经营现金流比 越高越好）。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np


# 因子方向：+1 = 越大越好，-1 = 越小越好（与 fundamental_scoring 一致）
DEFAULT_DIRECTIONS: Dict[str, int] = {
    'forward_pe': -1, 'peg': -1, 'pb': -1, 'debt_ratio': -1,
    'roe': +1, 'net_profit_growth': +1, 'dividend_yield': +1, 'ocf_ratio': +1,
}

# 默认权重（复用 fundamental_scoring.DEFAULT_WEIGHTS，缺失则等权）
def _default_weights() -> Dict[str, float]:
    # 注:原导入名 `FACTOR_WEIGHTS` 在 fundamental_scoring 里不存在(只有 DEFAULT_WEIGHTS),
    # 恒触发 ImportError → 静默退化等权,使 balanced 档(及所有未显式传权重的调用)从不按
    # 设计的差异化权重(ROE 20%/PE 15%…)打分。改用真名 DEFAULT_WEIGHTS。
    try:
        from fundamental_scoring import DEFAULT_WEIGHTS
        return dict(DEFAULT_WEIGHTS)
    except Exception:
        return {k: 1.0 for k in DEFAULT_DIRECTIONS}


# =============================================================================
# 引擎：去极值 / 标准化 / 合成 / 排名
# =============================================================================
def winsorize(s: pd.Series, lower: float = 0.05, upper: float = 0.95) -> pd.Series:
    """分位数去极值（默认 5%/95%）。"""
    s = pd.to_numeric(s, errors='coerce')
    if s.notna().sum() < 3:
        return s
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lower=lo, upper=hi)


def zscore(s: pd.Series) -> pd.Series:
    """横截面 Z-score 标准化（均值0方差1）；缺失填 0（中性）。"""
    s = pd.to_numeric(s, errors='coerce')
    mu, sd = s.mean(), s.std(ddof=0)
    if sd is None or sd == 0 or np.isnan(sd):
        return pd.Series(0.0, index=s.index)
    return ((s - mu) / sd).fillna(0.0)


def composite_score(factor_df: pd.DataFrame,
                    weights: Optional[Dict[str, float]] = None,
                    directions: Optional[Dict[str, int]] = None,
                    do_winsorize: bool = True) -> pd.DataFrame:
    """对因子矩阵做 去极值→方向调整→Z-score→加权合成。

    Args:
        factor_df: index=股票代码，columns=因子名，values=因子原始值。
        weights:   因子权重（缺省复用 fundamental_scoring 权重 / 等权）。
        directions:因子方向 {因子:+1/-1}（缺省用 DEFAULT_DIRECTIONS，未知因子默认 +1）。
    Returns:
        DataFrame：原始因子 + 每因子 `z_<因子>` + `composite` 合成分，按 composite 降序。
    """
    weights = weights or _default_weights()
    directions = directions or DEFAULT_DIRECTIONS
    df = factor_df.copy()
    use_factors = [c for c in df.columns if c in weights]
    if not use_factors:
        use_factors = list(df.columns)
        weights = {c: 1.0 for c in use_factors}

    wsum = sum(abs(weights.get(f, 0)) for f in use_factors) or 1.0
    composite = pd.Series(0.0, index=df.index)
    for f in use_factors:
        raw = winsorize(df[f]) if do_winsorize else df[f]
        z = zscore(raw) * directions.get(f, +1)   # 方向调整：越小越好的取负
        df[f'z_{f}'] = z
        composite = composite + z * (weights.get(f, 0) / wsum)
    df['composite'] = composite
    return df.sort_values('composite', ascending=False)


def rank_topn(factor_df: pd.DataFrame, n: int = 20,
              weights: Optional[Dict[str, float]] = None,
              directions: Optional[Dict[str, int]] = None) -> pd.DataFrame:
    """合成打分并取 TopN，附排名列。"""
    scored = composite_score(factor_df, weights, directions)
    scored.insert(0, 'rank', range(1, len(scored) + 1))
    return scored.head(n)


# =============================================================================
# 因子有效性检验（IC / IR）— 借鉴 factor-research
# =============================================================================
def factor_ic(factor: pd.Series, forward_return: pd.Series, method: str = 'spearman') -> float:
    """单期 IC：因子值与未来收益的横截面相关系数。

    默认 Rank IC（Spearman）——不依赖 scipy，用「先 rank 再 Pearson」等价实现；
    method='pearson' 则直接算普通相关。
    """
    a = pd.to_numeric(factor, errors='coerce')
    b = pd.to_numeric(forward_return, errors='coerce')
    df = pd.DataFrame({'f': a, 'r': b}).dropna()
    if len(df) < 5:
        return float('nan')
    if method == 'spearman':
        return float(df['f'].rank().corr(df['r'].rank(), method='pearson'))
    return float(df['f'].corr(df['r'], method='pearson'))


def ic_ir(ic_series: List[float]) -> Dict[str, float]:
    """多期 IC 序列 → IC 均值 / IC 标准差 / IR(=均值/标准差) / IC>0 占比。"""
    s = pd.Series([x for x in ic_series if x is not None and not np.isnan(x)], dtype='float64')
    if len(s) == 0:
        return {'ic_mean': float('nan'), 'ic_std': float('nan'), 'ir': float('nan'), 'positive_rate': float('nan')}
    mean, std = s.mean(), s.std(ddof=0)
    return {
        'ic_mean': round(float(mean), 4),
        'ic_std': round(float(std), 4),
        'ir': round(float(mean / std), 4) if std and std != 0 else float('nan'),
        'positive_rate': round(float((s > 0).mean()), 4),
        'periods': int(len(s)),
    }


# =============================================================================
# 可选 loader：指数成分股 + 因子取数（默认股票池，复用现有取数）
# =============================================================================
def get_index_universe(index_code: str = '000300') -> List[str]:
    """取指数成分股代码列表（默认沪深300）。优先 akshare 中证指数接口。

    常用：'000300' 沪深300 / '000905' 中证500 / '000852' 中证1000。
    失败返回空列表（调用方应降级）。
    """
    try:
        import akshare as ak
        from akshare_safe import call as ak_call   # 硬超时,防 csindex/新浪慢响应卡死
        try:
            df = ak_call(ak.index_stock_cons_csindex, symbol=index_code, timeout=20)
            col = '成分券代码' if '成分券代码' in df.columns else df.columns[4]
            return [str(x).zfill(6) for x in df[col].tolist()]
        except Exception:
            df = ak_call(ak.index_stock_cons, symbol=index_code, timeout=20)
            col = '品种代码' if '品种代码' in df.columns else df.columns[0]
            return [str(x).zfill(6) for x in df[col].tolist()]
    except Exception:
        return []


def get_sector_leaders(leaders_per_board: int = 2, max_boards: Optional[int] = None,
                       include_concept: bool = False) -> List[str]:
    """取常见板块龙头股：各行业板块按总市值排序取前 N 只。

    龙头定义：行业板块内总市值最大的若干只（市值≈行业地位/龙头属性）。
    leaders_per_board: 每个板块取前几只；max_boards: 限制板块数（调试/限速用）。
    include_concept: 是否额外纳入概念板块龙头（默认否，概念噪声大）。
    失败/无 akshare 返回空列表（调用方降级）。

    ⚠️ 2026-06-27 防东财封禁重构:本函数原裸 `import akshare` 逐 ~86 个行业板块调
    `ak.stock_board_industry_cons_em`(均走东财 push2),无缓存/无限流/无超时,单次冷算 ~87 次
    东财调用,被 webui「强制刷新」/MCP 选股盘中连点放大成 IP 级封禁源。现:① 结果整体缓存 20h
    (板块龙头名单日内基本不变,盘后焐一次各处复用);② 每板块调用走 akshare_safe(硬超时)+
    rate_limiter('akshare') 背压(把 86 次背靠背突发拉成 3s 间隔,杜绝突发触封);③ 盘中现拉路径
    由上游 screen_index_cached(cache_only=盘中) 短路拦截,本函数盘中不冷算。"""
    leaders: List[str] = []
    try:
        from cache import cache_get, cache_set
    except Exception:
        cache_get = cache_set = None
    ckey = f"sector_leaders:{leaders_per_board}:{max_boards}:{int(bool(include_concept))}"
    if cache_get:
        hit = cache_get(ckey)
        if isinstance(hit, list) and hit:
            return hit
    try:
        import akshare as ak
        from akshare_safe import call as ak_call
        from rate_limiter import throttle as _throttle
    except Exception:
        return []

    def _topn_of_board(cons_df) -> List[str]:
        if cons_df is None or len(cons_df) == 0:
            return []
        cap_col = next((c for c in ('总市值', '流通市值') if c in cons_df.columns), None)
        code_col = next((c for c in ('代码', '股票代码') if c in cons_df.columns), None)
        if not code_col:
            return []
        df = cons_df.sort_values(cap_col, ascending=False) if cap_col else cons_df
        return [str(x).zfill(6) for x in df[code_col].head(leaders_per_board).tolist()]

    try:
        boards = ak_call(ak.stock_board_industry_name_em, timeout=20)
        name_col = '板块名称' if '板块名称' in boards.columns else boards.columns[0]
        names = boards[name_col].tolist()
        if max_boards:
            names = names[:max_boards]
        for nm in names:
            _throttle('akshare')   # 背压:东财板块成分接口逐个 3s 间隔,防突发封禁
            try:
                leaders += _topn_of_board(ak_call(ak.stock_board_industry_cons_em, symbol=nm, timeout=20))
            except Exception:
                continue
    except Exception:
        pass

    if include_concept:
        try:
            cb = ak_call(ak.stock_board_concept_name_em, timeout=20)
            ncol = '板块名称' if '板块名称' in cb.columns else cb.columns[0]
            cnames = cb[ncol].tolist()[:(max_boards or 30)]
            for nm in cnames:
                _throttle('akshare')
                try:
                    leaders += _topn_of_board(ak_call(ak.stock_board_concept_cons_em, symbol=nm, timeout=20))
                except Exception:
                    continue
        except Exception:
            pass

    out = list(dict.fromkeys(leaders))  # 去重保序
    if cache_set and out:
        cache_set(ckey, out, 20 * 3600)   # 20h:盘后焐后存活整个交易日,次日盘后焐自然刷新
    return out


def get_default_universe(index_code: str = '000300', add_sector_leaders: bool = True,
                         leaders_per_board: int = 2, max_boards: Optional[int] = None) -> Dict[str, Any]:
    """默认股票池 = 指数成分股 ∪ 各行业板块龙头股（去重）。

    用户反馈：仅指数成分覆盖不够，叠加常见板块龙头以增强行业代表性。
    返回 {'symbols': [...], 'from_index': n, 'from_leaders': m, 'total': k}。
    """
    idx = get_index_universe(index_code)
    leaders = get_sector_leaders(leaders_per_board, max_boards) if add_sector_leaders else []
    merged = list(dict.fromkeys(idx + leaders))
    return {
        'symbols': merged,
        'from_index': len(idx),
        'from_leaders': len(leaders),
        'total': len(merged),
        'index_code': index_code,
    }


def build_factor_frame(symbols: List[str], progress: bool = True,
                       workers: int = 1) -> pd.DataFrame:
    """对股票列表逐只取因子（复用 fundamental_scoring.collect_factors），组装横截面矩阵。

    workers>1 时用线程池并发抓因子（每只是独立外部请求，IO 密集）。底层
    `core/rate_limiter` 仍按 host 限流提供背压，故并发安全且不至于触发封禁。
    Returns: index=股票代码，columns=因子名 的 DataFrame（缺失为 NaN）。
    """
    from fundamental_scoring import collect_factors
    import time as _time

    # 慢股阈值(秒): 单只取因子超过这个时间打 ⚠️, 方便定位"卡因子"的元凶。
    # collect_factors 内部对每只调 pywencai(单只最多 30s), 60 只串行最坏 30 分钟 —— 这是
    # unified_selection "取因子" 卡死的根因, 以前只有每 25 步粗进度, 看不出卡哪只。
    _SLOW = 5.0

    def _one(sym):
        try:
            return sym, collect_factors(sym)
        except Exception:
            return sym, {}

    rows = {}
    _t_all = _time.time()
    if workers and workers > 1 and len(symbols) > 1:
        from concurrent.futures import ThreadPoolExecutor
        done = 0
        with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as ex:
            for sym, fac in ex.map(_one, symbols):
                rows[sym] = fac
                done += 1
                if progress and done % 25 == 0:
                    print(f"   取因子 {done}/{len(symbols)} ...(并发{workers}, 累计{_time.time()-_t_all:.0f}s)", flush=True)
    else:
        for i, sym in enumerate(symbols):
            if progress and i % 25 == 0:
                print(f"   取因子 {i}/{len(symbols)} ...(累计{_time.time()-_t_all:.0f}s)", flush=True)
            _t0 = _time.time()
            rows[sym] = _one(sym)[1]
            _dt = _time.time() - _t0
            if progress and _dt >= _SLOW:
                print(f"   ⚠️ 取因子慢: {sym} 耗时 {_dt:.1f}s", flush=True)
    if progress:
        print(f"   取因子完成 {len(symbols)}/{len(symbols)} (总耗时 {_time.time()-_t_all:.0f}s)", flush=True)
    # 保持入参顺序（线程池乱序返回 → 按 symbols 重排，不影响打分但利于复现）
    return pd.DataFrame.from_dict({s: rows.get(s, {}) for s in symbols}, orient='index')


def screen_index(index_code: str = '000300', n: int = 20,
                 weights: Optional[Dict[str, float]] = None,
                 limit: Optional[int] = None,
                 add_sector_leaders: bool = True,
                 leaders_per_board: int = 2,
                 universe: Optional[List[str]] = None,
                 workers: int = 1) -> Dict[str, Any]:
    """端到端：(指数成分 ∪ 板块龙头) → 取因子 → 多因子打分 → TopN。

    universe: 直接指定股票池（如选股器海选结果）；给定则忽略 index/leaders。
    add_sector_leaders: 默认在指数成分上叠加各行业板块龙头（用户反馈增强）。
    limit: 调试时只取前 N 只，避免全量取数耗时。
    workers: >1 时并发取因子（显著提速，见 build_factor_frame）。
    """
    if universe is None:
        u = get_default_universe(index_code, add_sector_leaders, leaders_per_board)
        universe = u['symbols']
        pool_meta = {k: u[k] for k in ('from_index', 'from_leaders', 'total')}
    else:
        pool_meta = {'from_index': 0, 'from_leaders': 0, 'total': len(universe)}
    if not universe:
        return {'error': f'无法获取股票池（指数 {index_code} / 板块龙头均失败）', 'top': []}
    if limit:
        universe = universe[:limit]
    factor_df = build_factor_frame(universe, workers=workers)
    ranked = rank_topn(factor_df, n=n, weights=weights)
    keep = ['rank', 'composite'] + [c for c in factor_df.columns]
    return {
        'index_code': index_code,
        'pool': pool_meta,
        'universe_size': len(universe),
        'factors_used': list(factor_df.columns),
        'top': ranked[[c for c in keep if c in ranked.columns]].reset_index()
                     .rename(columns={'index': 'symbol'}).to_dict(orient='records'),
    }


# =============================================================================
# 带缓存的端到端选股（webui / jobs 共用一个缓存键，避免每次现算 30-60s）
# =============================================================================
# 同一指数的"已打分股票池"缓存 6 小时。结果按需切片到 n，故缓存不含 n。
_CANON_LIMIT = 60          # 规范股票池上限（指数成分 ∪ 龙头取前 N，控时长）
_CACHE_TTL = 6 * 3600      # 6 小时（盘中变动有限，且周扫会刷新）


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec='seconds')


# 风格预设:在同一份缓存因子池上重新加权(几乎零成本)。balanced=等权(默认)。
STYLE_WEIGHTS: Dict[str, Optional[Dict[str, float]]] = {
    'balanced': None,
    'value':    {'forward_pe': 2.0, 'pb': 2.0, 'peg': 1.5, 'dividend_yield': 1.5,
                 'debt_ratio': 1.0, 'roe': 1.0, 'net_profit_growth': 0.5, 'ocf_ratio': 1.0},
    'growth':   {'net_profit_growth': 2.5, 'roe': 1.5, 'peg': 1.5, 'ocf_ratio': 1.0,
                 'forward_pe': 0.5, 'pb': 0.5, 'debt_ratio': 0.7, 'dividend_yield': 0.3},
    'quality':  {'roe': 2.0, 'ocf_ratio': 1.5, 'debt_ratio': 1.5, 'net_profit_growth': 1.5,
                 'dividend_yield': 1.0, 'forward_pe': 1.0, 'pb': 1.0, 'peg': 1.0},
    'dividend': {'dividend_yield': 2.5, 'debt_ratio': 1.5, 'ocf_ratio': 1.5, 'roe': 1.5,
                 'forward_pe': 1.0, 'pb': 1.0, 'peg': 0.5, 'net_profit_growth': 0.5},
}


def _rescore_pool(rows: List[Dict], weights: Dict[str, float], n: int) -> List[Dict]:
    """用 weights 在已打分池(含各因子原始值)上重排,返回新 top-n。复用缓存,无需联网。"""
    if not rows:
        return []
    factors = [c for c in DEFAULT_DIRECTIONS if c in rows[0]]
    df = pd.DataFrame(rows).set_index('symbol')
    scored = composite_score(df[factors], weights=weights)
    by_sym = {r['symbol']: r for r in rows}
    out = []
    for i, sym in enumerate(list(scored.index)[:n], 1):
        r = dict(by_sym.get(sym, {'symbol': sym}))
        r['rank'] = i
        r['composite'] = round(float(scored.loc[sym, 'composite']), 4)
        out.append(r)
    return out


def screen_index_cached(index_code: str = '000300', n: int = 15,
                        add_sector_leaders: bool = True, limit: int = _CANON_LIMIT,
                        workers: int = 8, force: bool = False,
                        ttl: int = _CACHE_TTL, style: str = 'balanced',
                        cache_only: bool = False) -> Dict[str, Any]:
    """带 Redis 缓存的 screen_index。同指数共享一份"已打分池",按 n 切片返回。

    - force=True：跳过读缓存（强制现算），仍写回（供 jobs 周扫预热）。
    - Redis 不可用 → 等价直算，绝不报错（cache 层已优雅降级）。
    - 返回在 screen_index 基础上加 `cached`(bool) / `cached_at`(ISO)。
    """
    try:
        from cache import cache_get, cache_set
    except Exception:
        cache_get = cache_set = None
    key = f"mf_screen:{index_code}:{limit}:{int(bool(add_sector_leaders))}"
    base = None
    if cache_get and not force:
        hit = cache_get(key)
        if isinstance(hit, dict) and hit.get('top'):
            base = hit
            base['cached'] = True
    if base is None:
        if cache_only:
            # cache_only:盘后预热缓存冷(失败/Redis挂)→ 早盘不现算 300 只,直接跳过(空 top)
            return {'top': [], 'index_code': index_code, 'cached': False,
                    'cache_only_miss': True, 'style': style}
        base = screen_index(index_code=index_code, n=max(n, _CANON_LIMIT),
                            limit=limit, add_sector_leaders=add_sector_leaders, workers=workers)
        if base.get('top'):
            base['cached_at'] = _now_iso()
            if cache_set:
                cache_set(key, base, ttl)
        base['cached'] = False
    # 切片到请求的 n（缓存保存的是规范池的全量打分）。指定风格则在全量池上重新加权。
    out = dict(base)
    pool = base.get('top') or []
    sw = STYLE_WEIGHTS.get(style)
    if sw:
        out['top'] = _rescore_pool(pool, sw, n)
        out['style'] = style
    else:
        out['top'] = pool[:n]
        out['style'] = 'balanced'
    return out


if __name__ == '__main__':
    # 自测：合成 50 只股票 × 4 因子，验证引擎与 IC/IR（无需联网）
    rng = np.random.RandomState(42)
    syms = [f'{600000+i}' for i in range(50)]
    fdf = pd.DataFrame({
        'forward_pe': rng.uniform(5, 60, 50),
        'roe': rng.uniform(-5, 30, 50),
        'net_profit_growth': rng.uniform(-30, 120, 50),
        'debt_ratio': rng.uniform(10, 80, 50),
    }, index=syms)

    top = rank_topn(fdf, n=5)
    print("=== TopN 选股（合成数据）===")
    print(top[['rank', 'composite', 'forward_pe', 'roe', 'net_profit_growth', 'debt_ratio']].round(2).to_string())

    # IC/IR：构造一个与未来收益正相关的因子
    future_ret = pd.Series(0.02 * (fdf['roe'] - fdf['roe'].mean()) / 10 + rng.normal(0, 0.03, 50), index=syms)
    ics = [factor_ic(fdf['roe'] + rng.normal(0, 3, 50), future_ret) for _ in range(12)]
    print("\n=== roe 因子 IC/IR（12 期模拟）===")
    print(ic_ir(ics))
