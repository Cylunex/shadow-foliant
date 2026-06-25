import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""自动化任务开关系统

设计目标：所有定时/工作流任务默认关闭，用户在 Admin UI 一键开启。
开关存储优先级：
  1. PG/SQLite 表 automation_switches（可在 UI 修改，运行时生效）
  2. env var AUTOMATION_<NAME>=true/false（无 DB 记录时回退）
  3. 内置默认（全部 False，保守起步）

注册了哪些自动化看 REGISTRY 字典。

接口：
  is_enabled(name)              查询
  set_enabled(name, on)         开关
  list_all()                    列所有 + 状态
  register(name, meta)          动态注册（启动时调）
"""

import os
from datetime import datetime
from typing import Dict, List, Any, Optional

from db_compat import connect as db_connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')


# 内置自动化清单（name -> meta）
REGISTRY: Dict[str, Dict[str, Any]] = {
    # 2026-06-12 整合:morning_briefing_push 已并入 morning_strategy;northbound_flow_refresh 已删。
    # 开关名 = hub.register 的任务名(对齐后 webui 开关经 jobs_hub._wrap 闸门真正生效)。
    # core=True 标记"常驻核心任务"(默认开,建议别关;关掉会影响下游)。
    # ===== 常驻核心:盘前/盘中/盘后主流程(原先不在开关表里 → webui 看不到,现补全)=====
    'morning_strategy': {
        'cn': '☀️ 晨间市场报告',
        'schedule': '09:00 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '8 维数据(龙虎榜/美股/新闻/北向/题材/FRED/A股大盘/持仓扫描)综合 AI → 开盘策略+持仓建议+昨日收益,4条推送',
    },
    'unified_selection': {
        'cn': '🎯 综合选股 TOP15',
        'schedule': '09:45 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '5策略+InStock13(进化参数)+组合新策略+多因子 并池打分,带来源标签/持仓标记',
    },
    'morning_portfolio': {
        'cn': '☀️ 早盘持仓分析',
        'schedule': '09:50 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '持仓逐只风险分+浮盈+买点+盘中异动(实时价,零逐只接口);并挑今日 top15 重点候选存 focus_candidates',
    },
    'noon_portfolio': {
        'cn': '🕦 午间重点盯盘',
        'schedule': '11:20 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '只看早盘挑出的 top15 重点候选(批量行情,零逐只K线)+ 持仓急跌兜底(原 stock_monitor 的急跌移此)',
    },
    'mx_selection_review': {
        'cn': '🔍 选股过妙想第二意见',
        'schedule': '10:30 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '对综合选股 TOP 逐个过东财妙想诊断',
    },
    'noon_report': {
        'cn': '📊 午盘简报',
        'schedule': '12:00 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '大盘/板块/热门股 + 持仓盘中概况(红绿/领涨领跌)',
    },
    'afternoon_portfolio': {
        'cn': '🧹 尾盘持仓总结',
        'schedule': '14:30 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '尾盘持仓三合一(原 持仓分析+AI体检+清仓助手):一次AI出 瘦身策略+逐只融合动作+尾盘机会;尾接止盈阶梯减仓[alert]',
    },
    'kline_prefetch': {
        'cn': '📥 K线缓存预热',
        'schedule': '16:30 每日(盘后链头)',
        'category': '数据', 'default': True,
        'description': '盘后全量预拉 持仓+监测+沪深300成分 日线写共享磁盘缓存(db/kline_cache),让回测/因子/晨报/持仓守卫命中暖缓存(0ms);全量拉天然无复权漂移,无需增量/周末特判',
    },
    'portfolio_indicator_snapshot': {
        'cn': '📸 持仓指标快照',
        'schedule': '16:45 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '持仓+监测列表算 MyTT/缠论/VaR 快照(早盘晨报扫描全靠它);尾接风险预警/形态告警[开关]。关掉会让次日分析读不到快照',
    },
    'daily_market_snapshot': {
        'cn': '📷 大盘快照',
        'schedule': '16:48 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '大盘+北向+龙虎榜快照入库',
    },
    'factor_collection': {
        'cn': '🧬 因子采集',
        'schedule': '16:40 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '收盘后采集 OHLCV+技术指标+估值打分因子快照',
    },
    'dragon_tiger_archive': {
        'cn': '🐉 龙虎榜归档',
        'schedule': '18:30 每日(晚间出全量)',
        'category': '核心', 'default': True, 'core': True,
        'description': '每交易日盘后拉龙虎榜存库(不做 AI)',
    },
    'daily_pnl_snapshot': {
        'cn': '💰 当日盈亏快照',
        'schedule': '22:30 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '合并股票日涨跌+基金日收益落 daily_pnl_snapshots(晨报昨日收益的数据源)',
    },
    'weekly_analysis': {
        'cn': '📊 周日持仓综合周报',
        'schedule': '周日 15:00',
        'category': '核心', 'default': True, 'core': True,
        'description': '评级变化/减仓加仓Top5/4象限体检/已实现盈亏/周末新闻',
    },
    'weekly_db_cleanup': {
        'cn': '🧹 每周数据库清理',
        'schedule': '周一 03:00',
        'category': '核心', 'default': True, 'core': True,
        'description': '清理过期分析记录 + VACUUM(SQLite)',
    },
    'mx_daily_analysis': {
        'cn': '🌙 妙想收盘复盘',
        'schedule': '17:00 每日',
        'category': '核心', 'default': True, 'core': True,
        'description': '收盘后东财妙想一站式复盘(收数据→调妙想→格式化)推送',
    },
    'mx_weekend_outlook': {
        'cn': '🔮 周末妙想研判',
        'schedule': '周日 10:00',
        'category': '核心', 'default': True,
        'description': '周末用东财妙想做前瞻:本周复盘+下周展望+热点题材+重点行业,充分利用周末空档(非交易日照跑)',
    },
    # ===== 常驻核心(已在表内)=====
    'ai_rec_check': {
        'cn': '📊 推荐池胜率回填',
        'schedule': '16:35 每日(盘后)',
        'category': '核心',
        'default': True,  # 盈利反馈环数据引擎:盘后收盘价后验 → 填 last_price/realized_pnl,喂周评估;非盯盘非短线
        'description': '盘后用收盘价对 AI 推荐池对比目标/止损 + 回填真实盈亏(2026-06-25 由盘中每30分改盘后)',
    },
    'ai_eval_weekly': {
        'cn': 'AI 推荐周度评估推送',
        'schedule': '周一 09:30',
        'category': '核心',
        'default': True,  # 盈利反馈环:按 source 出真实盈亏评估,回喂选股决策;无 AI 调用,开销低
        'description': '按 source 评估过去 30 天推荐真实盈亏(胜率/平均收益/盈亏比)',
    },
    'decision_signal_outcomes': {
        'cn': '🎯 决策信号后验校验',
        'schedule': '16:55 每日',
        'category': '数据',
        'default': True,  # 统一信号层后验:K线判 hit/miss,累积按维度胜率;无 AI 调用,纯库+K线缓存
        'description': '对已过持有周期的决策信号用 K线判命中,累积按动作/来源/周期的真实胜率',
    },
    'selection_debate': {
        'cn': '⚔️ 选股红蓝对抗证伪',
        'schedule': '已并入 unified_selection(9:45),仅手动/MCP',
        'category': '核心',
        'default': False,  # 2026-06-25 已并入 unified_selection,不再单独注册;模块保留供手动触发
        'description': '[已并入 9:45 综合选股,不再定时] 多头/空头/裁判对抗,结论进决策信号后验',
    },
    'portfolio_stress_ai': {
        'cn': '🛡️ 组合压力情景叙事官',
        'schedule': '周日 16:00',
        'category': '核心',
        'default': True,  # scenario_stress 已写好8情景却无人调;AI 翻译成最脆弱情景+风险担当+减仓对冲预案
        'description': '周末跑全8宏观情景压力+集中度,AI 给最脆弱情景/风险担当持仓/具体减仓对冲建议',
    },
    'announcement_scan': {
        'cn': '📢 公告事件分级',
        'schedule': '18:35 每日(三合一)',
        'category': '核心',
        'default': True,  # announcements 端点零调用;AI 分类+重大性分级,利空强→reduce信号+告警(黑天鹅预警)
        'description': '对持仓+选股拉近5天公告,AI 分类+利好利空强度分级,利空强即时告警并写决策信号',
    },
    'lockup_radar': {
        'cn': '⏳ 持仓解禁雷达',
        'schedule': '已并入 announcement_scan(18:35),仅手动/MCP',
        'category': '核心',
        'default': False,  # 2026-06-25 已并入 announcement_scan 三合一,不再单独注册;模块保留供手动
        'description': '[已并入 announcement_scan,不再定时] 查持仓未来60天解禁,AI 给减仓研判',
    },
    'research_digest': {
        'cn': '📑 研报增量解读',
        'schedule': '已并入 announcement_scan(18:35),仅手动/MCP',
        'category': '核心',
        'default': False,  # 2026-06-25 已并入 announcement_scan 三合一,不再单独注册;模块保留供手动
        'description': '[已并入 announcement_scan,不再定时] 拉近10天券商研报,AI 提炼评级方向/核心逻辑',
    },
    # exit_advice / portfolio_health_ai 已并入 afternoon_portfolio(尾盘持仓总结 eod_review),
    # 不再单独定时;模块仍供 MCP(exit_advice/portfolio_health_check)与前端"🧹清仓助手"页按需调用。
    'stock_monitor_check': {
        'cn': '📊 持仓进场区间监控',
        'schedule': '已退役(2026-06-25),仅手动/MCP',
        'category': '核心',
        'default': False,  # 2026-06-25 退役:盯进场区间价值低,不再注册;急跌兜底已并入 11:20 noon_portfolio
        'description': '[已退役,不再定时] 检查监控股是否进入进场区间;急跌监控已移到 noon_portfolio',
    },
    'daily_backtest': {
        'cn': '📐 盘后策略回测',
        'schedule': '19:00 每日',
        'category': '核心',
        'default': True,
        'description': '盘后对持仓TOP10跑5套核心策略回测，推送胜率/收益',
    },

    # 新增工作流（默认全关）
    'wf_selection_to_rec': {
        'cn': '🔗 工作流：综合选股 → 战绩追踪',
        'schedule': '09:45 每日(unified_selection 内)',
        'category': '工作流',
        'default': True,  # 零成本(只记录不监控不调AI),给选股算真实胜率,反哺门槛
        'description': '综合选股 TOP10 入推荐池记录(source=unified_selection),ai_eval_weekly 出真实胜率',
    },
    'wf_overnight_to_rec': {
        'cn': '🔗 工作流 A：晨间策略 → AI 推荐池',
        'schedule': '09:00 每日(morning_strategy 内)',
        'category': '工作流',
        'default': True,
        'description': '把 morning_strategy 输出的 candidate_stocks 自动入推荐池 + 启用监控(喂盈利闭环)',
    },
    'wf_daily_strategy_scan': {
        'cn': '🔗 工作流 B：盘后策略扫描 → AI 分析 → 推荐池',
        'schedule': '16:30 每日(daily_backtest 尾部,进化后用最新基因组情报)',
        'category': '工作流',
        'default': True,  # 盈利闭环的"新血"来源:每日产出带目标/止损的 AI 推荐供监控+评估
        'description': '对持仓+候选池跑 InStock 10 策略，命中股深度 AI 分析(带盈亏比约束+历史战绩反馈)后入推荐',
    },
    'wf_weekly_backtest': {
        'cn': '🔗 工作流 C：周末回测推送',
        'schedule': '周日 20:00',
        'category': '工作流',
        'default': True,
        'description': '对 10 套策略跑过去 30 天回测，推送"最有效策略" TOP 5',
    },
    'wf_daily_pattern_alert': {
        'cn': '🔗 工作流 D：形态+基本面 E 级预警',
        'schedule': '15:45 每日(portfolio_indicator_snapshot 内,复用同一份K线)',
        'category': '工作流',
        'default': True,
        'description': '对持仓股扫 TA-Lib 反转形态 + 基本面 E 级警报，立即推送',
    },
    # ↓↓↓ 个人策略专属（按你 7 条规则定制）
    'wf_daily_candidate_pool': {
        'cn': '🎯 个人策略：每日候选池',
        'schedule': '09:45 每日(unified_selection 尾部)',
        'category': '个人策略',
        'default': True,
        'description': '低价(≤20)+非ST+强势板块+短期/历史低位+反转形态，TOP 10 推送',
    },
    # wf_weekly_portfolio_report 已并入 weekly_analysis(周日 15:00,4象限体检随周报常开)
    'wf_position_guard_check': {
        'cn': '🎯 个人策略：盘中加仓信号',
        'schedule': '已退役(2026-06-25 随 stock_monitor),仅手动/MCP',
        'category': '个人策略',
        'default': False,  # 2026-06-25 随 stock_monitor_check 退役(加仓决策权在用户);模块保留供手动
        'description': '[已退役,不再定时] 触发跌幅时审核 ✅建议加/⚠️警告止损',
    },
    'wf_position_profit_check': {
        'cn': '🎯 个人策略：减仓信号（方案 A）',
        'schedule': '14:30 每日(afternoon_portfolio 尾部)',
        'category': '个人策略',
        'default': True,
        'description': '30/60/100% 阶梯减仓 + 跌破 MA20 减半 / 跌破 MA60 清仓',
    },

    # ↓↓↓ 基金模块（长期/定投）
    'fund_nav_refresh': {
        'cn': '🏦 基金：盘后净值入库',
        'schedule': '22:00 每日',
        'category': '基金',
        'default': True,
        'description': '对持有基金 + 定投计划标的拉最新净值落 fund_nav 缓存',
    },
    'fund_dca_reminder': {
        'cn': '🏦 基金：定投到期提醒',
        'schedule': '08:55 每日',
        'category': '基金',
        'default': True,
        'description': '检查启用的定投计划,到当期定投日则提醒(阶段一只提醒不自动下单/记账)',
    },
    'fund_target_check': {
        'cn': '🏦 基金：定投止盈检查',
        'schedule': '22:05 每日',
        'category': '基金',
        'default': True,
        'description': '对设了止盈目标的持有基金,按最新净值算浮盈,达标则提醒赎回',
    },
    'fund_valuation_signal': {
        'cn': '🏦 基金：宽基估值分位播报',
        'schedule': '09:05 每日',
        'category': '基金',
        'default': True,
        'description': '扫常用宽基指数滚动PE分位,低估的提示加投(估值定投择时依据)',
    },
    'pg_backup': {
        'cn': '💾 运维：PG 全量备份到本地 SQLite',
        'schedule': '02:00 每日',
        'category': '运维',
        'default': True,
        'description': '把生产 PostgreSQL 所有表备份到 db/pg_backup.db(离线副本/灾备)',
    },
    'rag_ingest': {
        'cn': '🔎 运维：语义检索语料摄取',
        'schedule': '02:30 每日',
        'category': '运维',
        'default': True,
        'description': '历史分析/新闻/推荐 嵌入(BGE-M3)入 pgvector,保持语义搜索语料新鲜',
    },
}


def _init_table():
    if USE_POSTGRES:
        return
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS automation_switches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            enabled     INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            note        TEXT
        )
    ''')
    conn.commit()
    conn.close()


_init_table()


def _read_db(name: str) -> Optional[bool]:
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT enabled FROM automation_switches WHERE name = ?', (name,))
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return bool(row[0])
    except Exception:
        return None


def is_enabled(name: str) -> bool:
    """三层 fallback：DB > env > default"""
    db_val = _read_db(name)
    if db_val is not None:
        return db_val
    env_val = os.getenv(f'AUTOMATION_{name.upper()}', '').strip().lower()
    if env_val in ('1', 'true', 'yes', 'on'):
        return True
    if env_val in ('0', 'false', 'no', 'off'):
        return False
    meta = REGISTRY.get(name, {})
    return bool(meta.get('default', False))


def set_enabled(name: str, on: bool, note: str = '') -> bool:
    """更新 DB，立即生效"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute('''
            INSERT INTO automation_switches(name, enabled, note, updated_at)
            VALUES (?, ?, ?, NOW())
            ON CONFLICT(name) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                note = EXCLUDED.note,
                updated_at = NOW()
        ''', (name, bool(on) if USE_POSTGRES else (1 if on else 0), note))
    else:
        cur.execute('''
            INSERT INTO automation_switches(name, enabled, note)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                enabled = excluded.enabled,
                note = excluded.note,
                updated_at = CURRENT_TIMESTAMP
        ''', (name, 1 if on else 0, note))
    conn.commit()
    conn.close()
    return True


def list_all() -> List[Dict[str, Any]]:
    """返回所有自动化任务清单 + 当前状态"""
    out = []
    for name, meta in REGISTRY.items():
        out.append({
            'name': name,
            'cn': meta.get('cn', name),
            'schedule': meta.get('schedule', ''),
            'category': meta.get('category', '其他'),
            'description': meta.get('description', ''),
            'default': meta.get('default', False),
            'core': meta.get('core', False),
            'enabled': is_enabled(name),
        })
    return out


def get_recent_runs(name: str, limit: int = 10) -> List[Dict]:
    """从 job_runs 拉某 task 最近运行记录"""
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            SELECT started_at, finished_at, status, error
            FROM job_runs WHERE job_name = ?
            ORDER BY id DESC LIMIT ?
        ''', (name, limit))
        rows = cur.fetchall()
        conn.close()
        return [{'started_at': r[0], 'finished_at': r[1],
                 'status': r[2], 'error': r[3]} for r in rows]
    except Exception:
        return []


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== 自动化开关系统自检 ===')
    print(f'已注册: {len(REGISTRY)} 项')
    for item in list_all():
        flag = '🟢' if item['enabled'] else '⚪'
        print(f"  {flag} [{item['category']:>4s}] {item['cn']:35s} | {item['schedule']}")
