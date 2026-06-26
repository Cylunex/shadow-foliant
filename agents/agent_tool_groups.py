"""
Agent 工具分组 — 借鉴 go-stock tool_groups.go 设计

将数据采集按业务域预组合，Agent 直接调用 collect_* 拿到完整 context。
好处：
  1. 单一职责：每个 Agent 只关心自己业务域的数据
  2. 集中容错：底层数据源不可用时统一返回降级值，Agent 不用处处 try/except
  3. 节省 token：Agent prompt 只接收相关数据，避免冗余字段

每个 group 暴露：
  - meta: 组描述（给 Agent 路由用）
  - collect(symbol, **kwargs): 数据采集主入口
"""

from typing import Dict, Any, List


# =============================================================================
# 工具组元数据 — Agent 可据此决定激活哪些组
# =============================================================================
TOOL_GROUP_META: Dict[str, Dict[str, Any]] = {
    'base': {
        'description': '股票基础信息（代码、名称、市场、行业、当前价、涨跌幅）',
        'use_cases': ['任何分析的起点'],
    },
    'kline_technical': {
        'description': 'K线数据 + 技术指标（MA/RSI/MACD/KDJ + MyTT 通达信指标）',
        'use_cases': ['技术分析师', '盯盘 Agent', '形态识别'],
    },
    'chan_theory': {
        'description': '缠论（缠中说禅）结构：去包含/分型/笔/中枢/背驰 + 一二三类买卖点',
        'use_cases': ['技术分析师', '形态识别', '短线买卖点'],
    },
    'fund_flow': {
        'description': '资金流向（主力/超大单/中小单 + adata 北向资金）',
        'use_cases': ['资金面分析师', '主力选股', '北向跟踪'],
    },
    'fundamentals': {
        'description': '财务三表 + 季报 + 估值数据',
        'use_cases': ['基本面分析师', '价值股筛选'],
    },
    'sentiment': {
        'description': '新闻 / 龙虎榜 / 北向 / 概念热度 / 融资融券 / 强势股+题材归因',
        'use_cases': ['市场情绪解码', '游资跟踪', '题材识别'],
    },
    'chipset': {
        'description': '筹码层（融资融券明细 + 大宗交易 + 股东户数变化 + 筹码分布获利盘/成本 — 资金面+成本面闭环）',
        'use_cases': ['资金面分析师', '风险管理师', '主力筹码追踪'],
    },
    'macro_us': {
        'description': '美国宏观面板（FRED + yfinance fallback：利率/通胀/就业/VIX/美元）',
        'use_cases': ['宏观分析师', '凌晨综合策略', '外部影响评估'],
    },
    'risk': {
        'description': '限售解禁 / 大股东减持 / 重要事项（问财数据）',
        'use_cases': ['风险管理师'],
    },
}


# =============================================================================
# 工具组实现
# =============================================================================

def collect_base_context(symbol: str) -> Dict[str, Any]:
    """采集股票基础信息"""
    ctx: Dict[str, Any] = {'symbol': symbol, 'errors': []}
    try:
        from stock_data import StockDataFetcher
        fetcher = StockDataFetcher()
        info = fetcher.get_stock_info(symbol)
        if isinstance(info, dict):
            ctx['info'] = info
    except Exception as e:
        ctx['errors'].append(f'stock_info: {e}')
    return ctx


def collect_kline_technical_context(symbol: str, period: str = '1y',
                                    pattern_lookback: int = 10) -> Dict[str, Any]:
    """采集 K线 + 完整技术指标（MyTT 12 通达信指标）+ TA-Lib 61 K线形态"""
    ctx: Dict[str, Any] = {'symbol': symbol, 'period': period, 'errors': []}
    df = None
    try:
        from stock_data import StockDataFetcher
        fetcher = StockDataFetcher()
        df = fetcher.get_stock_data(symbol, period, adjust='qfq')  # 技术分析用前复权
        if isinstance(df, dict) and df.get('error'):
            ctx['errors'].append(f"get_stock_data: {df['error']}")
            return ctx
        df_ind = fetcher.calculate_technical_indicators(df)
        if isinstance(df_ind, dict) and df_ind.get('error'):
            ctx['errors'].append(f"calc_indicators: {df_ind['error']}")
            return ctx
        ctx['indicators'] = fetcher.get_latest_indicators(df_ind)
        ctx['df_tail'] = df_ind.tail(30).to_dict(orient='records') if hasattr(df_ind, 'tail') else None
    except Exception as e:
        ctx['errors'].append(f'kline_technical: {e}')

    try:
        if df is not None and hasattr(df, '__len__') and len(df) >= 120:
            from pattern_recognition import PatternDetector
            det = PatternDetector()
            if det.available:
                raw = det.detect_all(df, lookback=pattern_lookback)
                if 'error' in raw:
                    ctx['errors'].append(f"pattern: {raw['error']}")
                else:
                    patterns = []
                    for pid, r in raw.items():
                        if pid == 'support_resistance' or not isinstance(r, dict):
                            continue
                        if r.get('found') and r.get('name'):
                            patterns.append({
                                'id': pid,
                                'name': r['name'],
                                'type': r['type'],
                                'date': r.get('date'),
                                'days_ago': r.get('days_ago', 0),
                                'strength': r.get('strength'),
                            })
                    patterns.sort(key=lambda x: x['days_ago'])
                    ctx['patterns'] = patterns
                    ctx['support_resistance'] = raw.get('support_resistance')
    except Exception as e:
        ctx['errors'].append(f'pattern_detect: {e}')

    # 行情阶段 regime + 策略信号(缩量回踩/底部放量/情绪顶)— 供 AI 随行情判断、防接飞刀
    try:
        if df is not None and hasattr(df, '__len__') and len(df) >= 25:
            from strategy_signals import shrink_pullback, bottom_volume, emotion_top_warning, detect_regime
            ctx['regime'] = detect_regime(df)
            ctx['signals'] = {
                'shrink_pullback': shrink_pullback(df),
                'bottom_volume': bottom_volume(df),
                'emotion_top_warning': emotion_top_warning(df),
            }
    except Exception as e:
        ctx['errors'].append(f'strategy_signals: {e}')

    return ctx


def collect_chan_theory_context(symbol: str, period: str = '1y') -> Dict[str, Any]:
    """采集缠论结构：去包含 → 分型 → 笔 → 中枢 → 背驰 → 一二三类买卖点

    复用 kline 数据源（StockDataFetcher.get_stock_data），纯本地计算，无外部依赖。
    返回 chan_theory.analyze_chan() 的结构化结果（含 summary 中文摘要供 Agent prompt 注入）。
    """
    ctx: Dict[str, Any] = {'symbol': symbol, 'period': period, 'errors': []}
    try:
        from stock_data import StockDataFetcher
        from chan_theory import analyze_chan
        df = StockDataFetcher().get_stock_data(symbol, period, adjust='qfq')  # 缠论用前复权
        if isinstance(df, dict) and df.get('error'):
            ctx['errors'].append(f"get_stock_data: {df['error']}")
            return ctx
        ctx['chan'] = analyze_chan(df, symbol)
    except Exception as e:
        ctx['errors'].append(f'chan_theory: {e}')
    return ctx


def collect_fund_flow_context(symbol: str, days: int = 60) -> Dict[str, Any]:
    """采集资金流：个股资金流 + 北向资金大盘 + adata 历史日度"""
    ctx: Dict[str, Any] = {'symbol': symbol, 'errors': []}

    # 个股资金流（沿用现有 a-stock HTTP > akshare > tushare 优先级）
    try:
        from fund_flow_akshare import FundFlowAkshareDataFetcher
        ff = FundFlowAkshareDataFetcher(days=days)
        ctx['individual_flow'] = ff.get_fund_flow(symbol)
    except Exception as e:
        ctx['errors'].append(f'individual_flow: {e}')

    # adata 历史日度资金流（备用,走 datahub 统一数据层熔断/超时）
    try:
        import datahub
        rows = datahub.capital_flow_adata(symbol)
        ctx['adata_capital_flow'] = rows[:days] if rows else []
    except Exception as e:
        ctx['errors'].append(f'adata_capital_flow: {e}')

    # 北向资金大盘趋势
    try:
        import datahub
        ctx['north_flow'] = datahub.north_flow(days)
    except Exception as e:
        ctx['errors'].append(f'north_flow: {e}')

    return ctx


def _valuation_verdict(fv: Dict[str, Any]) -> Dict[str, str]:
    """对 full_valuation 做规则化判断（PEG/消化年数）

    判断依据（a-stock-data 框架）：
      PEG: <1 便宜 / 1-1.5 合理 / >1.5 贵 / 无（增速<=0 或无 EPS 预期）
      消化年数: <2 合理 / 2-4 偏贵 / >4 太贵
    """
    peg = fv.get('peg')
    digest = fv.get('digest_years')
    pe_fwd = fv.get('pe_fwd')

    if peg is None:
        peg_label = 'N/A（缺增速或一致预期）'
    elif peg < 1:
        peg_label = '便宜 (PEG<1)'
    elif peg <= 1.5:
        peg_label = '合理 (PEG 1~1.5)'
    else:
        peg_label = '贵 (PEG>1.5)'

    if digest is None or digest == 0:
        digest_label = '无需消化（pe_fwd<=30 或缺数据）'
    elif digest < 2:
        digest_label = f'合理（{digest}年）'
    elif digest <= 4:
        digest_label = f'偏贵（{digest}年）'
    else:
        digest_label = f'太贵（{digest}年）'

    if pe_fwd is None:
        action = '估值不可计算 — 建议看其他维度'
    elif peg is not None and peg < 1 and (digest or 0) < 2:
        action = '基本面估值有吸引力'
    elif peg is not None and peg > 2:
        action = '估值偏高 — 等待回调或减仓'
    else:
        action = '估值中性 — 看趋势/资金面决策'

    return {'peg_label': peg_label, 'digest_label': digest_label, 'action': action}


def collect_fundamentals_context(symbol: str) -> Dict[str, Any]:
    """采集基本面：三表 + 季报 + 估值"""
    ctx: Dict[str, Any] = {'symbol': symbol, 'errors': []}
    try:
        from stock_data import StockDataFetcher
        fetcher = StockDataFetcher()
        ctx['financial'] = fetcher.get_financial_data(symbol)
    except Exception as e:
        ctx['errors'].append(f'financial: {e}')

    try:
        from quarterly_report_data import QuarterlyReportData
        ctx['quarterly'] = QuarterlyReportData().fetch(symbol)
    except ImportError:
        pass
    except Exception as e:
        ctx['errors'].append(f'quarterly: {e}')

    try:
        import datahub
        ctx['valuation'] = datahub.valuation(symbol)
    except Exception as e:
        ctx['errors'].append(f'valuation: {e}')

    try:
        import datahub
        fv = datahub.full_valuation(symbol)
        if fv:
            ctx['full_valuation'] = fv
            ctx['valuation_verdict'] = _valuation_verdict(fv)
    except Exception as e:
        ctx['errors'].append(f'full_valuation: {e}')

    return ctx


def _aggregate_hot_themes(hot_df, top_n: int = 20) -> List[Dict[str, Any]]:
    """从同花顺热点 DataFrame 聚合"题材归因"标签，输出热度榜

    reason 列形如 "算力租赁+Token工厂+AI政务"，按 +/、/空格 切割。
    返回: [{'theme': 'AI算力', 'count': 18}, ...]
    """
    from collections import Counter
    if hot_df is None or len(hot_df) == 0:
        return []
    counter: Counter = Counter()
    reason_col = '题材归因' if '题材归因' in hot_df.columns else 'reason'
    if reason_col not in hot_df.columns:
        return []
    for raw in hot_df[reason_col].dropna():
        if not isinstance(raw, str):
            continue
        s = raw.replace('、', '+').replace(' ', '').replace(',', '+').replace('，', '+')
        for t in s.split('+'):
            t = t.strip()
            if t:
                counter[t] += 1
    return [{'theme': t, 'count': c} for t, c in counter.most_common(top_n)]


def collect_sentiment_context(symbol: str = None, lookback_days: int = 30) -> Dict[str, Any]:
    """采集市场情绪：新闻 / 龙虎榜 / 北向 / 融资融券 / 同花顺热点+题材"""
    ctx: Dict[str, Any] = {'symbol': symbol, 'errors': []}

    try:
        import datahub
        ctx['dragon_tiger_today'] = datahub.dragon_tiger()
    except Exception as e:
        ctx['errors'].append(f'dragon_tiger: {e}')

    try:
        import datahub
        ctx['north_flow'] = datahub.north_flow(lookback_days)
    except Exception as e:
        ctx['errors'].append(f'north_flow: {e}')

    try:
        import datahub
        hot_df = datahub.hot_stocks()
        if hot_df is not None and hasattr(hot_df, 'empty') and not hot_df.empty:
            ctx['hot_stocks'] = hot_df.head(30).to_dict(orient='records')
            ctx['hot_themes'] = _aggregate_hot_themes(hot_df, top_n=20)
    except Exception as e:
        ctx['errors'].append(f'hot_stocks: {e}')

    if symbol:
        try:
            import datahub
            ctx['margin'] = datahub.margin(symbol)
        except Exception as e:
            ctx['errors'].append(f'margin: {e}')

        try:
            import datahub
            ctx['news'] = datahub.stock_news(symbol, 20)
        except Exception as e:
            ctx['errors'].append(f'news: {e}')

    return ctx


def collect_chipset_context(symbol: str) -> Dict[str, Any]:
    """采集筹码三件套：融资融券明细 / 大宗交易 / 股东户数变化

    全部来自 eastmoney datacenter-web，同源稳定，构成资金面闭环：
      - margin_trading: 融资余额/买入/偿还 + 融券（杠杆资金动向）
      - block_trade: 大宗交易成交价/量 + 买卖方营业部（筹码换手）
      - holder_num_change: 股东户数季度变化（筹码集中度）
    """
    ctx: Dict[str, Any] = {'symbol': symbol, 'errors': []}

    try:
        import datahub
        ctx['margin_trading'] = datahub.margin(symbol, 30)
    except Exception as e:
        ctx['errors'].append(f'margin_trading: {e}')

    try:
        import datahub
        ctx['block_trade'] = datahub.block_trade(symbol)
    except Exception as e:
        ctx['errors'].append(f'block_trade: {e}')

    try:
        import datahub
        ctx['holder_num_change'] = datahub.holder_num_change(symbol)
    except Exception as e:
        ctx['errors'].append(f'holder_num_change: {e}')

    # 筹码分布(获利盘/平均成本/成本区间/集中度)— 由K线本地估算,补成本面闭环
    try:
        from stock_data import StockDataFetcher
        from chip_distribution import chip_distribution
        df = StockDataFetcher().get_stock_data(symbol, '1y', adjust='qfq')  # 筹码分布用前复权
        if not isinstance(df, dict):
            ctx['chip_distribution'] = chip_distribution(df)
    except Exception as e:
        ctx['errors'].append(f'chip_distribution: {e}')

    return ctx


def collect_macro_us_context(symbol: str = None) -> Dict[str, Any]:
    """采集美国宏观面板（FRED API + yfinance fallback）

    无 symbol 依赖，纯市场层面。返回 snapshot + 文本摘要。
    """
    ctx: Dict[str, Any] = {'errors': []}
    try:
        from fred_economic_data import get_fed_snapshot, format_snapshot
        snap = get_fed_snapshot()
        ctx['fred_snapshot'] = snap
        ctx['fred_summary_text'] = format_snapshot(snap)
        ctx['fred_api_configured'] = bool(__import__('os').getenv('FRED_API_KEY', '').strip())
    except Exception as e:
        ctx['errors'].append(f'fred_snapshot: {e}')
    return ctx


def collect_risk_context(symbol: str) -> Dict[str, Any]:
    """采集风险数据：限售解禁 / 大股东减持 / 重要事项"""
    ctx: Dict[str, Any] = {'symbol': symbol, 'errors': []}
    try:
        from risk_data_fetcher import RiskDataFetcher
        ctx['risk'] = RiskDataFetcher().fetch(symbol)
    except ImportError:
        pass
    except Exception as e:
        ctx['errors'].append(f'risk_data: {e}')

    try:
        import datahub
        ctx['lockup'] = datahub.lockup_expiry(symbol)
    except Exception as e:
        ctx['errors'].append(f'lockup: {e}')

    return ctx


# =============================================================================
# 工具组分发器 — Agent 可按需激活
# =============================================================================

GROUP_COLLECTORS = {
    'base': collect_base_context,
    'kline_technical': collect_kline_technical_context,
    'chan_theory': collect_chan_theory_context,
    'fund_flow': collect_fund_flow_context,
    'fundamentals': collect_fundamentals_context,
    'sentiment': collect_sentiment_context,
    'chipset': collect_chipset_context,
    'macro_us': collect_macro_us_context,
    'risk': collect_risk_context,
}


def collect(groups, symbol: str, **kwargs) -> Dict[str, Any]:
    """按工具组列表批量采集 context

    用法:
        ctx = collect(['base', 'kline_technical', 'fund_flow'], '600519')
        # ctx == {'base': {...}, 'kline_technical': {...}, 'fund_flow': {...}}
    """
    if isinstance(groups, str):
        groups = [groups]
    result = {}
    for g in groups:
        fn = GROUP_COLLECTORS.get(g)
        if fn is None:
            result[g] = {'error': f'unknown group: {g}'}
            continue
        try:
            result[g] = fn(symbol, **kwargs) if g != 'sentiment' else fn(symbol=symbol)
        except Exception as e:
            result[g] = {'error': str(e)}
    return result


# Agent 推荐工具组映射 — Agent 实现可参考此清单选择激活
AGENT_RECOMMENDED_GROUPS = {
    'technical': ['base', 'kline_technical', 'chan_theory'],
    'fundamental': ['base', 'fundamentals'],
    'fund_flow': ['base', 'kline_technical', 'fund_flow'],
    'sentiment': ['base', 'sentiment'],
    'risk': ['base', 'risk'],
    'news': ['base', 'sentiment'],
    'longhubang': ['sentiment'],
    'sector_strategy': ['fund_flow', 'sentiment'],
    'smart_monitor': ['base', 'kline_technical'],
}
