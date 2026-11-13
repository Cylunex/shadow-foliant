"""Project-owned personal CLI contract. Discovery never loads database/network modules."""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, create_model

Code = Annotated[str, Field(pattern=r'^\d{6}$')]
Text = Annotated[str, Field(max_length=200)]
Limit = Annotated[int, Field(ge=1, le=500)]
Days = Annotated[int, Field(ge=1, le=3650)]
Key = Annotated[str, Field(min_length=8, max_length=160)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    target: str
    model: Any
    effect: str = 'read'

    def public(self):
        return {'name': self.name, 'description': self.description,
                'inputSchema': self.model.model_json_schema(),
                'effect': self.effect, 'annotations': {'readOnlyHint': self.effect == 'read'}}


CAPABILITIES: dict[str, Capability] = {}


def register(capability_name, description, target, *, effect='read', schema_extra=None, **fields):
    config = ConfigDict(extra='forbid', strict=True, json_schema_extra=schema_extra)
    model = create_model(capability_name.replace('.', '_'), __config__=config, **fields)
    CAPABILITIES[capability_name] = Capability(capability_name, description, target, model, effect)


register('trades.list', '成交记录，按实际成交时间倒序；查最后一笔用 limit=1，成交日与导入日不同。', '$portfolio.trades',
         code=(Code | Literal[''], ''), trade_date=(Text, ''), import_date=(Text, ''), limit=(Limit, 200))
trade_fields = dict(rows=(Annotated[list[dict[str, Any]], Field(max_length=500)], []),
                    table=(Annotated[str, Field(max_length=200000)], ''), update_position=(bool, True))
register('trades.preview', '后台校验已成交事实、名称解析、防重及持仓变动，不提交成交。', '@preview_trades', **trade_fields)
register('trades.import', '录入用户已完成的成交并读回回执；同一意图重试复用 idempotency_key，不是证券下单。',
         '@import_trades', effect='write', idempotency_key=(Key, ...), **trade_fields)
register('trades.receipt', '按 batch_id 或原 idempotency_key 之一查询当前身份的成交导入状态和回执。',
         '@trade_receipt', schema_extra={'minProperties': 1, 'maxProperties': 1},
         batch_id=(Text, Field(default_factory=lambda: None)),
         idempotency_key=(Key, Field(default_factory=lambda: None)))
register('portfolio.summary', '真实持仓、数量与成本；不是券商可卖余额。', '$portfolio.summary')
register('portfolio.intraday', '今日已持久化的持仓、正式 TOP5/TOP15 与行情质量，不重新分析。', '$portfolio.intraday_decision')
register('portfolio.performance', '组合 TWR、XIRR、回撤与盈亏归因，读取已有净值和成交。', 'performance:summary')
register('portfolio.daily-pnl', '按收盘口径查看每日、当月及区间盈亏，区分真实账本与预览。', 'application.account_preview:account_books', days=(Days, 30))
register('portfolio.curve', '股票组合净值快照曲线。', 'portfolio_snapshot:get_snapshots', limit=(Limit, 90))
register('portfolio.habits', '持有时长、交易频率与变动时间线。', '@portfolio_habits', since_days=(Days, 90))
register('portfolio.classify', '持仓健康、观察、警报及数据不足分组，默认不调用基本面重分析。', 'portfolio_classifier:classify_all',
         limit=(Annotated[int, Field(ge=1, le=60)], 30), with_fundamental=(bool, False))
register('portfolio.actions', '持仓加减仓信号和约束，不执行证券交易。', '@portfolio_actions', limit=(Annotated[int, Field(ge=1, le=60)], 40))
register('market.overview', '已保存市场总览。', '$market.read')
register('market.quality', '行情与证据质量，保留缺失及陈旧状态。', '$data_quality.read')
register('market.indices', '主要指数点位、涨跌幅与供应商时刻。', 'datahub:indices')
register('market.news', '市场财经快讯。', 'datahub:market_news', page_size=(Annotated[int, Field(ge=1, le=100)], 30))
register('market.sectors', '行业或概念板块强弱。', 'datahub:sector_ranking', sector_type=(Literal['industry', 'concept'], 'industry'), top_n=(Limit, 15))
register('stock.info', 'A 股基本信息与行情。', 'datahub:stock_info', code=(Code, ...))
register('stock.news', '个股新闻及来源。', 'datahub:stock_news', code=(Code, ...), page_size=(Annotated[int, Field(ge=1, le=100)], 20))
register('stock.research', '按需个股研究；quick 为默认，deep 会消耗模型预算。', '$security_research.compatibility_research',
         symbol=(Code, ...), depth=(Literal['quick', 'deep'], 'quick'))
register('stock.research-latest', '最近正式个股研究，不重新分析。', '$security_research.latest_formal', symbol=(Code, ...))
register('stock.backtest', '单股策略历史回测，保留样本期和费用口径，不发布参数。', '@stock_backtest', code=(Code, ...), strategy=(Text, 'enter'), hold_days=(Annotated[int, Field(ge=1, le=250)], 10), stop_pct=(Positive, 8.0), target_pct=(Positive, 15.0), lookback_days=(Annotated[int, Field(ge=30, le=730)], 365))
register('selection.latest', '最近正式选股结果、TOP5/TOP15 和证据，不重新选股。', '$selection.latest_formal')
register('selection.premarket', '盘前事实与引用，仅作参考，不改变正式候选。', 'news_flow.premarket_facts:latest_premarket_facts', report_date=(Text, ''))
register('monitors.list', '查看盯盘配置。', 'agent_operations:list_monitors')
register('monitors.upsert', '添加或修改盯盘；dry_run 默认预览，用户明确委托后提交并回读。', 'agent_operations:upsert_monitor', effect='write',
         code=(Code, ...), name=(Text, ''), rating=(Text, '持有'), entry_low=(Positive | None, None), entry_high=(Positive | None, None),
         take_profit=(Positive | None, None), stop_loss=(Positive | None, None), check_interval=(Annotated[int, Field(ge=10, le=86400)], 60),
         notification_enabled=(bool, True), trading_hours_only=(bool, True), dry_run=(bool, True))
register('monitors.remove', '移除指定盯盘配置，默认预览。', 'agent_operations:remove_monitor', effect='write', code=(Code, ...), dry_run=(bool, True))
register('recommendations.list', '活跃推荐及监控、止盈止损状态。', 'agent_operations:list_recommendations', code=(Code | Literal[''], ''), monitored_only=(bool, False), limit=(Limit, 100))
register('recommendations.monitor', '将指定推荐加入盯盘，默认预览。', 'agent_operations:enable_recommendation_monitor', effect='write', rec_id=(Annotated[int, Field(gt=0)], ...), dry_run=(bool, True))
register('recommendations.close', '关闭指定研究推荐，不卖出持仓，默认预览。', 'agent_operations:close_recommendation', effect='write',
         rec_id=(Annotated[int, Field(gt=0)], ...), reason=(Text, 'manual'), close_price=(Positive | None, None), dry_run=(bool, True))
register('signals.list', '查看决策信号；服务可能顺带标记已到期信号。', 'decision_signal:list_signals',
         code=(Code | Literal[''], ''), status=(Text, 'active'), action=(Text, ''), source_type=(Text, ''), days=(Days, 30), limit=(Limit, 100))
register('signals.changes', '实质动作变化及时间。', 'decision_signal:list_transitions', code=(Code | Literal[''], ''), source_type=(Text, ''), material_only=(bool, True), days=(Days, 30), limit=(Limit, 100))
register('signals.winrate', '已有方向后验，不能当作含费用可执行收益。', 'decision_signal:outcome_stats', dimension=(Literal['action', 'source_type', 'horizon'], 'action'), days=(Days, 180))
register('signals.status', '归档或关闭指定信号，默认预览。', 'agent_operations:set_signal_status', effect='write',
         signal_id=(Annotated[int, Field(gt=0)], ...), status=(Literal['closed', 'archived', 'invalidated', 'expired'], ...), dry_run=(bool, True))
register('strategy.parameters', '当前个人策略参数、含义与默认值。', 'user_strategy_config:list_all')
register('strategy.update', '调整单个既有个人参数；expected_value 防止覆盖别人改动，默认预览。', '@update_strategy', effect='write',
         key=(Text, ...), value=(float | int | bool, ...), expected_value=(float | int | bool, ...), reason=(Annotated[str, Field(min_length=1, max_length=500)], ...), dry_run=(bool, True))
register('strategy.history', '当前操作身份的个人策略修改回执，包含原因及修改前后值。', '@strategy_history', limit=(Limit, 30))
register('strategy.deployment', '当前实际生效的基础变体及组合策略，不发布新策略。', 'analysis.strategy_genome:get_live_strategy_set')
register('strategy.evidence', '研究实验、模型账本与成熟度，不自动晋级正式政策。', '@strategy_evidence')
register('tasks.list', '任务计划、开关、最近运行。', '@list_tasks')
register('tasks.runs', '已提交任务的运行记录。', 'jobs.task_control:list_task_runs', task_name=(Text, ''), limit=(Limit, 30))
register('tasks.status', '任务终态和结果；queued/running 不等于完成。', 'jobs.task_control:get_task_run', run_id=(Text, ...))
register('fund.holdings', '基金账本持仓。', 'fund_db:get_holdings')
register('fund.transactions', '基金已记录申赎流水。', 'fund_db:get_transactions', code=(Code | None, None))
register('fund.plans', '基金定投计划。', 'fund_db:get_plans', only_enabled=(bool, False))
register('fund.info', '场外基金基本资料。', 'fund_data:get_fund_basic', code=(Code, ...))
register('fund.nav', '最新已确认净值，不能当作实时估值。', 'fund_data:latest_nav', code=(Code, ...))
register('fund.compare', '多只基金共同窗口比较。', 'fund_analysis:compare_funds', codes=(Annotated[list[Code], Field(min_length=2, max_length=10)], ...), lookback_days=(Days, 1095))
register('fund.dca-backtest', '定投历史模拟，不生成真实申赎。', '@fund_backtest', code=(Code, ...), amount=(Positive, 1000.0), period=(Literal['weekly', 'monthly'], 'monthly'), day=(Annotated[int, Field(ge=1, le=28)], 5), strategy=(Literal['normal', 'valuation', 'value_avg'], 'normal'))
register('assets.overview', '股票与基金组合视图，区分成本和市值口径。', '@asset_overview', basis=(Literal['cost', 'market'], 'cost'))
register('system.cockpit', '已有快照、任务异常及数据源健康。', 'jobs.task_control:agent_cockpit', recent_limit=(Annotated[int, Field(ge=1, le=30)], 5), compact=(bool, True))


def services():
    from application.runtime import get_application_services
    return get_application_services()


def catalog(config):
    return [cap.public() for cap in CAPABILITIES.values() if cap.effect == 'read' or config.get('allow_writes') is True]


def invoke(name, args, config):
    cap = CAPABILITIES.get(name)
    if cap is None or (cap.effect != 'read' and config.get('allow_writes') is not True):
        raise ValueError('capability_unavailable')
    values = cap.model.model_validate(args).model_dump()
    if cap.target.startswith('@'):
        return globals()[cap.target[1:]](config=config, **values)
    if cap.target.startswith('$'):
        service, method = cap.target[1:].split('.')
        return getattr(getattr(services(), service), method)(**values)
    module, function = cap.target.split(':')
    return getattr(importlib.import_module(module), function)(**values)


def preview_trades(*, rows, table, update_position, config):
    if bool(rows) == bool(table.strip()):
        raise ValueError('provide_rows_or_table')
    return services().trade_entry.preview(rows=rows, table=table, update_position=update_position,
                                          actor_id=config['actor'], persist=False)


def import_trades(*, rows, table, update_position, idempotency_key, config):
    if bool(rows) == bool(table.strip()):
        raise ValueError('provide_rows_or_table')
    # Reuse the domain's durable batch and atomic commit, without exposing an approval UI.
    entry = services().trade_entry
    fields = {'trades': rows, 'table': table, 'updatePosition': update_position}
    try:
        batch = entry.create_review(actor_id=config['actor'], fields=fields, idempotency_key=idempotency_key, source='skill:foliant-personal-portfolio')
    except Exception as exc:
        if getattr(exc, 'code', '') == 'invalid_trade':
            return preview_trades(rows=rows, table=table, update_position=update_position, config=config)
        raise
    result = entry.commit_review(batch['review_id'], actor_id=config['actor'], idempotency_key=idempotency_key, source='skill:foliant-personal-portfolio')
    receipt = trade_receipt(batch_id=batch['review_id'], config=config)
    return {**receipt, 'replayed': bool(batch.get('replayed') or result.get('replayed'))}


def trade_receipt(*, batch_id=None, idempotency_key=None, config):
    if (batch_id is None) == (idempotency_key is None):
        raise ValueError('provide_batch_id_or_idempotency_key')
    entry = services().trade_entry
    if batch_id is not None:
        batch = entry.get_review(batch_id, actor_id=config['actor'])
    else:
        batch = entry.get_review_by_key(idempotency_key, actor_id=config['actor'])
    result = entry._review_envelope(batch)
    return {'status': 'complete' if result['state'] == 'approved' else result['state'],
            'batch_id': result['review_id'], 'receipt': result['receipt'],
            'record_count': len(result['fields']['trades']), 'records': result['fields']['trades'],
            'update_position': result['fields']['updatePosition']}


def portfolio_habits(*, since_days, config):
    import portfolio_insights as pi
    return {'duration': pi.holding_duration_distribution(), 'frequency': pi.trading_frequency_analysis(since_days),
            'timeline': pi.portfolio_change_timeline(since_days, limit=200)}


def portfolio_actions(*, limit, config):
    import position_guardian as pg
    import position_profit_taker as pt
    return {'add': pg.evaluate_all_triggered(limit=limit, with_fundamental=False), 'reduce': pt.evaluate_all(limit=limit)}


def list_tasks(*, config):
    from automation_config import list_all
    from jobs.task_control import enrich_task_catalog
    return enrich_task_catalog(list_all())


def strategy_evidence(*, config):
    from application.decision_loop import DecisionLoopService
    return DecisionLoopService().dashboard()


def fund_backtest(*, code, amount, period, day, strategy, config):
    import fund_data
    import fund_dca
    frame = fund_data.get_nav_history(code)
    if frame is None or frame.empty:
        return {'status': 'missing', 'code': code}
    return fund_dca.dca_backtest(frame, amount=amount, period=period, day=day, strategy=strategy)


def update_strategy(*, key, value, expected_value, reason, dry_run, config):
    from application.personal_strategy import update_parameter
    return update_parameter(key, value, expected_value, reason, dry_run, actor=config['actor'])


def asset_overview(*, basis, config):
    import fund_portfolio
    return fund_portfolio.combined_market_view() if basis == 'market' else fund_portfolio.combined_asset_view()


def stock_backtest(*, code, strategy, hold_days, stop_pct, target_pct, lookback_days, config):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from stock_data import StockDataFetcher
    from backtest_engine import backtest_one
    frame = StockDataFetcher().get_stock_data(code, '2y')
    if frame is None or isinstance(frame, dict) or frame.empty:
        return {'status': 'missing', 'code': code}
    end = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    return backtest_one(code, strategy, frame, str(end - timedelta(days=lookback_days)), str(end),
                        hold_days=hold_days, stop_pct=stop_pct, target_pct=target_pct)


def strategy_history(*, limit, config):
    from application.personal_strategy import parameter_history
    return parameter_history(actor=config['actor'], limit=limit)
