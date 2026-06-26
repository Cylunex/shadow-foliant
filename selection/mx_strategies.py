"""妙想智能选股的「5 大策略镜像」—— 与问财 5 策略一一对应的自然语言查询,作**非问财冗余源**。

走妙想 selectSecurity 海选(analysis/miaoxiang.screen),结果入 strategy_cache 当日缓存
(盘前 strategy_prefetch 预取 + 09:45 unified_selection 读暖,与问财策略同机制、同避高峰)。

设计:source 名带「妙想·」前缀,与问财源(主力资金/低价擒牛…)区分 → 同一只票若问财源 + 妙想源
都命中,在 unified 的 _add 里得 +2 分、命中 2 source → 排序靠前(**双源交叉验证 = 更可信**)。
问财熔断时妙想这 5 条仍能出候选,整层选股不至于因单一数据源(问财)挂掉而哑火。
查询可按需调整(改这里即可;缓存按 name 键,改查询次日生效或 use_cache=False 立即生效)。
"""

# (缓存名/source 标签, 妙想自然语言查询, select_type)
MX_STRATEGIES = [
    ('妙想·主力',   '主力资金净流入排名前20的A股', 'A股'),
    ('妙想·低价',   '股价低于15元、近期主力资金净流入、成交活跃的A股，按主力净流入排名前20', 'A股'),
    ('妙想·小市值', '总市值30到80亿、净利润同比正增长、ROE为正的小市值A股，前20', 'A股'),
    ('妙想·净利',   '净利润同比增长超过50%、营业收入同比增长的A股，按净利润增速排名前20', 'A股'),
    ('妙想·低估值', '市盈率0到15、市净率低于2、ROE较高的低估值A股，按市盈率从低到高前20', 'A股'),
]

_TOP_N = 20


def run_one(name: str, query: str, select_type: str = 'A股',
            use_cache: bool = True, top_n: int = _TOP_N):
    """跑一条妙想策略 → (ok, df, msg)。走 strategy_cache 当日缓存;df 列含 '代码'/'名称'。
    use_cache=False 强制现取回写(盘前预取用)。任何异常 → (False, None, msg),不抛。"""
    import strategy_cache as _sc

    def _fetch():
        try:
            import miaoxiang as _mx
            df = _mx.screen(query, select_type)
        except Exception as e:
            return False, None, f'{name} 异常: {type(e).__name__}: {str(e)[:50]}'
        if df is None or not hasattr(df, 'empty') or df.empty or '代码' not in df.columns:
            return False, None, f'{name} 无结果'
        df = df.head(top_n) if top_n else df
        return True, df, f'{name} {len(df)}只'

    try:
        return _sc.cached(name, _fetch, use_cache=use_cache)
    except Exception as e:
        return False, None, f'{name} 缓存异常: {type(e).__name__}: {str(e)[:50]}'


def run_all(use_cache: bool = True) -> dict:
    """跑全部 5 条妙想策略 → {name: (ok, df, msg)}。供 unified_selection 并池。"""
    out = {}
    for name, query, st in MX_STRATEGIES:
        out[name] = run_one(name, query, st, use_cache=use_cache)
    return out
