"""妙想智能选股的五组外部参考策略。

结果只用于发现、对照与后验评估，不得进入本地正式候选，也不得改变 TOP15/TOP5
成员或顺序。缓存按策略名保存；查询变化通过 query_hash 留痕。
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
        # 读 config.EXCLUDE_KCB(默认排除):排除时在 NL 查询里补"非科创板",妙想少海选 688(unified _add 也会兜底拦)
        q = query
        try:
            import config as _cfg
            _excl = bool(getattr(_cfg, 'EXCLUDE_KCB', True))
        except Exception:
            _excl = True
        if _excl and '科创' not in q:
            q = q + '，非科创板'
        try:
            import miaoxiang as _mx
            df = _mx.screen(q, select_type)
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


def run_all(use_cache: bool = True, top_n: int = 5) -> dict:
    """跑全部五条参考策略 → ``{name: (ok, df, msg)}``。"""
    out = {}
    for name, query, st in MX_STRATEGIES:
        out[name] = run_one(name, query, st, use_cache=use_cache, top_n=top_n)
    return out
