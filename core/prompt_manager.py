import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""Prompt 模板管理 CRUD

借鉴 go-stock 的 PromptTemplate 表设计 — 让 prompt 从代码硬编码解耦，
支持运行时增删改查 + 按 agent_type / scene 分类。

数据库表：prompt_templates（PG/SQLite 双兼容）
  - id, name (唯一), agent_type, scene, content, description, version
  - is_default, is_active, created_at, updated_at

预置场景（initial seed）：
  - 凌晨综合策略 (overnight_strategy)
  - 龙虎榜分析 (longhubang_analysis)
  - 智策板块 (sector_strategy)
  - 持仓周度分析 (portfolio_weekly)
  - 多分析师讨论 (analyst_debate)

接口：
  upsert(name, content, ...)  增改
  get(name)                   查
  list(agent_type=None, scene=None) 列表
  delete(name)                删
  set_default(name, scene)    标记为某场景默认
  render(scene, **vars)       拉默认模板 + 变量替换
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Any

from db_compat import connect as db_connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')


def _init_table():
    """SQLite 模式自动建表；PG 模式靠 init_postgres.sql"""
    if USE_POSTGRES:
        return
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL UNIQUE,
            agent_type   TEXT,
            scene        TEXT,
            content      TEXT NOT NULL,
            description  TEXT,
            version      INTEGER NOT NULL DEFAULT 1,
            is_default   INTEGER NOT NULL DEFAULT 0,
            is_active    INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


_init_table()


def _now() -> str:
    return datetime.now().isoformat()


def upsert(name: str, content: str,
           agent_type: str = '', scene: str = '',
           description: str = '', is_default: bool = False) -> Dict[str, Any]:
    """按 name 唯一约束 upsert；存在则 version+=1 + updated_at 刷新"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT version FROM prompt_templates WHERE name = ?', (name,))
    row = cur.fetchone()
    if row:
        new_ver = int(row[0]) + 1
        if USE_POSTGRES:
            cur.execute('''
                UPDATE prompt_templates SET
                    content = ?, agent_type = ?, scene = ?,
                    description = ?, version = ?, is_default = ?,
                    updated_at = NOW()
                WHERE name = ?
            ''', (content, agent_type, scene, description, new_ver,
                  bool(is_default) if USE_POSTGRES else (1 if is_default else 0), name))
        else:
            cur.execute('''
                UPDATE prompt_templates SET
                    content = ?, agent_type = ?, scene = ?,
                    description = ?, version = ?, is_default = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ?
            ''', (content, agent_type, scene, description, new_ver,
                  bool(is_default) if USE_POSTGRES else (1 if is_default else 0), name))
        action = 'updated'
    else:
        cur.execute('''
            INSERT INTO prompt_templates(name, agent_type, scene, content, description, is_default)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, agent_type, scene, content, description, bool(is_default) if USE_POSTGRES else (1 if is_default else 0)))
        action = 'created'

    if is_default and scene:
        false_lit = 'FALSE' if USE_POSTGRES else '0'
        cur.execute(f'''
            UPDATE prompt_templates SET is_default = {false_lit}
            WHERE scene = ? AND name != ?
        ''', (scene, name))

    conn.commit()
    conn.close()
    return {'name': name, 'action': action}


def get(name: str) -> Optional[Dict[str, Any]]:
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT id, name, agent_type, scene, content, description,
               version, is_default, is_active, created_at, updated_at
        FROM prompt_templates WHERE name = ?
    ''', (name,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_dict(row)


def list_all(agent_type: Optional[str] = None,
             scene: Optional[str] = None,
             only_active: bool = True) -> List[Dict[str, Any]]:
    sql = '''
        SELECT id, name, agent_type, scene, content, description,
               version, is_default, is_active, created_at, updated_at
        FROM prompt_templates WHERE 1=1
    '''
    params = []
    if agent_type:
        sql += ' AND agent_type = ?'; params.append(agent_type)
    if scene:
        sql += ' AND scene = ?'; params.append(scene)
    if only_active:
        sql += ' AND is_active = TRUE' if USE_POSTGRES else ' AND is_active = 1'
    sql += ' ORDER BY scene, name'
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def delete(name: str) -> bool:
    """软删 — 设 is_active=False/0；硬删请直接 SQL"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute('UPDATE prompt_templates SET is_active = FALSE WHERE name = ?', (name,))
    else:
        cur.execute('UPDATE prompt_templates SET is_active = 0 WHERE name = ?', (name,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def set_default(name: str, scene: str) -> bool:
    """把指定 name 设为某 scene 的默认（同 scene 其他清 default 标）"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute('UPDATE prompt_templates SET is_default = FALSE WHERE scene = ?', (scene,))
        cur.execute('UPDATE prompt_templates SET is_default = TRUE WHERE name = ?', (name,))
    else:
        cur.execute('UPDATE prompt_templates SET is_default = 0 WHERE scene = ?', (scene,))
        cur.execute('UPDATE prompt_templates SET is_default = 1 WHERE name = ?', (name,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def render(scene: str, fallback: Optional[str] = None, **vars) -> str:
    """拿场景默认模板，str.format(**vars) 替换变量；找不到时用 fallback"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    where_true = 'TRUE' if USE_POSTGRES else '1'
    cur.execute(f'''
        SELECT content FROM prompt_templates
        WHERE scene = ? AND is_default = {where_true} AND is_active = {where_true}
        LIMIT 1
    ''', (scene,))
    row = cur.fetchone()
    conn.close()
    template = row[0] if row else fallback
    if template is None:
        return ''
    try:
        return template.format(**vars)
    except (KeyError, IndexError):
        return template


def _row_to_dict(row) -> Dict[str, Any]:
    keys = ['id', 'name', 'agent_type', 'scene', 'content', 'description',
            'version', 'is_default', 'is_active', 'created_at', 'updated_at']
    d = dict(zip(keys, row))
    d['is_default'] = bool(d['is_default'])
    d['is_active'] = bool(d['is_active'])
    return d


def seed_defaults():
    """填充内置 prompt 模板（首次启动用）"""
    defaults = [
        {
            'name': 'overnight_strategy_v3',
            'agent_type': 'strategy_analyst',
            'scene': 'overnight_strategy',
            'description': '晨间综合策略 8 维数据分析（v3:并入A股大盘/板块/持仓扫描,含完整 JSON 输出结构）',
            'is_default': True,
            'content': """你是一名资深 A 股策略分析师。请基于以下 8 维数据，综合判断今日 A 股开盘策略。

【1. 昨日 ({lookback_date}) 龙虎榜净流入 TOP 10】
{dragon_tiger_summary}

【2. 美股隔夜收盘】
{us_summary}

【3. 隔夜国内新闻头条】
{news_summary}

【4. 北向资金近 5 日】
{north_summary}

【5. 昨日强势股 TOP 10 (含题材归因)】
{hot_summary}

【5b. 昨日题材热度榜 TOP 15 (按强势股出现频次)】
{themes_summary}

【6. 美国宏观面板（FRED + yfinance）— 利率/通胀/就业/VIX/美元】
{fred_summary}

【7. A股大盘指数】
{cn_index_summary}

【7b. 行业板块强弱】
{sector_summary}

【8. 我的持仓技术扫描（含昨日盈亏/浮盈/破位/缠论信号）】
{hold_summary}

请综合以上信息给出今日开盘策略，严格按 JSON 格式输出（不要任何说明文字）：
{{
    "lazy_summary": "3-4句口语化的今日操作要点（大盘基调/该卖谁该留谁/可加谁，像朋友间提醒，直接说人话）",
    "open_strategy": "1-2 句开盘整体策略（高开/低开应对）",
    "external_impact": "美股+北向资金+美宏观对 A 股的指引",
    "hot_sectors": ["板块1（原因）", "板块2（原因）"],
    "risk_warning": "需要规避的风险点",
    "candidate_stocks": [
        {{"code": "代码", "name": "名称", "reason": "推荐理由",
         "entry_low": 入场区间下沿数字, "entry_high": 入场区间上沿数字,
         "target_price": 目标价数字, "stop_loss": 止损价数字, "rating": "buy/strong_buy/hold"}}
    ],
    "position_advice": "针对第8维持仓逐只口语化建议（卖/减/留/可加+一句理由）",
    "confidence": "高/中/低"
}}""",
        },
        {
            'name': 'longhubang_default_v1',
            'agent_type': 'longhubang_analyst',
            'scene': 'longhubang_analysis',
            'description': '龙虎榜游资行为解读',
            'is_default': True,
            'content': '你是龙虎榜游资行为专家。基于昨日龙虎榜数据：\n{data_block}\n\n请从席位风格、净买额、概念归属三个维度分析。',
        },
        {
            'name': 'portfolio_weekly_v1',
            'agent_type': 'portfolio_analyst',
            'scene': 'portfolio_weekly',
            'description': '持仓周度健康度诊断',
            'is_default': True,
            'content': '你是持仓健康诊断专家。基于持仓数据：\n{data_block}\n\n请输出：每只标的的行动建议（持有/加仓/减仓/清仓）+ 整体组合健康度评分。',
        },
        {
            'name': 'analyst_debate_v1',
            'agent_type': 'meta',
            'scene': 'analyst_debate',
            'description': '多分析师讨论协调',
            'is_default': True,
            'content': '以下是 6 位 A 股分析师对 {symbol} 的独立分析：\n{analyses}\n\n请综合所有观点，给出最终的买入/持有/卖出建议 + 风险提示。',
        },
    ]
    for d in defaults:
        upsert(**d)
    return len(defaults)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== Prompt Manager 自检 ===')
    n = seed_defaults()
    print(f'已填充 {n} 个默认模板')
    print('\n所有模板:')
    for t in list_all():
        print(f"  [v{t['version']}] {t['name']:30s} scene={t['scene']:25s} default={t['is_default']}")
    print('\n按 scene 渲染:')
    text = render('overnight_strategy', data_block='【测试数据】')
    print(text)
