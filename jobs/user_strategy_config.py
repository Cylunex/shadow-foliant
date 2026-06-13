import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""用户策略个人化参数 KV 存储

存放用户专属策略的可调参数（仓位上限/加仓次数/基本面门槛/强势板块定义等）。
所有参数都有合理默认值，UI 中可即时修改且持久化到 PG/SQLite。

接口：
  get(key, default=None)    读
  set(key, value)            写（值会 json 编码）
  list_all()                 列所有当前生效配置
  reset(key)                 删除某 key 恢复默认
"""

import os
import json
from typing import Any, Dict, Optional, List

from db_compat import connect as db_connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')


# 默认配置 — 与对话中你确认的 4 个核心参数一致
DEFAULTS: Dict[str, Dict[str, Any]] = {
    'max_position_pct': {
        'value': 10.0,
        'type': 'float',
        'cn': '单股仓位上限 (%)',
        'description': '单只股票占总资金上限，超过即拒绝加仓推送（仍发警告）',
    },
    'max_add_times': {
        'value': 5,
        'type': 'int',
        'cn': '单股累计加仓次数上限',
        'description': '同一只股已加仓 N 次后，新触发只发警告不发推荐',
    },
    'fundamental_min_score': {
        'value': 50.0,
        'type': 'float',
        'cn': '加仓基本面打分门槛',
        'description': '基本面打分低于此值时，跌幅触发不再推"建议加仓"，改为"⚠️ 警告"',
    },
    'price_max': {
        'value': 20.0,
        'type': 'float',
        'cn': '候选池股价上限 (元)',
        'description': '候选池只筛股价 ≤ 此值的股票',
    },
    'hot_sector_top_n': {
        'value': 30,
        'type': 'int',
        'cn': '强势板块定义 — TOP N',
        'description': '近 5 日资金净流入排名前 N 视为强势板块（候选池过滤用）',
    },
    'short_term_low_pct': {
        'value': 5.0,
        'type': 'float',
        'cn': '短期低位阈值 (%)',
        'description': '距 60 日最低价 ≤ 此 % 视为"短期低位"',
    },
    'historical_low_pct': {
        'value': 10.0,
        'type': 'float',
        'cn': '历史低位阈值 (%)',
        'description': '距 1 年最低价 ≤ 此 % 视为"历史低位"',
    },
    'drop_trigger_pct_today': {
        'value': 2.0,
        'type': 'float',
        'cn': '当日跌幅加仓触发 (%)',
        'description': '持仓股当日跌幅超过此值触发加仓审核',
    },
    'drop_trigger_pct_holding': {
        'value': 5.0,
        'type': 'float',
        'cn': '持仓盈亏跌幅加仓触发 (%)',
        'description': '持仓盈亏跌幅超过此值触发加仓审核',
    },
    'profit_take_1_pct': {
        'value': 30.0,
        'type': 'float',
        'cn': '减仓阶梯 1 (%)',
        'description': '【方案 A】持仓涨 30%，推送"建议减 30%"（回本并部分锁定）',
    },
    'profit_take_2_pct': {
        'value': 60.0,
        'type': 'float',
        'cn': '减仓阶梯 2 (%)',
        'description': '【方案 A】持仓涨 60%，推送"建议再减 30%"（继续锁定盈利）',
    },
    'profit_take_3_pct': {
        'value': 100.0,
        'type': 'float',
        'cn': '减仓阶梯 3 (%)',
        'description': '【方案 A】持仓涨 100%，推送"建议再减 30%"（剩 10% 长持博梦想）',
    },
    'enable_ma_stop_loss': {
        'value': True,
        'type': 'bool',
        'cn': 'MA 趋势保护开关',
        'description': '开启后：跌破 MA20 推"减 50%"，跌破 MA60 推"清仓剩余"（避免回吐盈利）',
    },
    'ma_stop_short': {
        'value': 20,
        'type': 'int',
        'cn': 'MA 短期均线周期',
        'description': '跌破此 MA 触发"减 50%"信号（默认 MA20）',
    },
    'ma_stop_long': {
        'value': 60,
        'type': 'int',
        'cn': 'MA 长期均线周期',
        'description': '跌破此 MA 触发"清仓"信号（默认 MA60）',
    },
    'observation_drop_low': {
        'value': 10.0,
        'type': 'float',
        'cn': '观察区间跌幅下界 (%)',
        'description': '持仓跌幅 [low, high]% 划入"🟡 观察"',
    },
    'observation_drop_high': {
        'value': 25.0,
        'type': 'float',
        'cn': '观察区间跌幅上界 (%)',
        'description': '持仓跌幅超过此值且基本面 D/E 划入"🔴 警报"',
    },
}


def _init_table():
    if USE_POSTGRES:
        return
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS user_strategy_config (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT NOT NULL UNIQUE,
            value_json  TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


_init_table()


def get(key: str, default: Any = None) -> Any:
    """读取配置，找不到时回退默认 DEFAULTS[key].value，再不行返回 default"""
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT value_json FROM user_strategy_config WHERE key = ?', (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    if key in DEFAULTS:
        return DEFAULTS[key]['value']
    return default


def set(key: str, value: Any) -> bool:
    """写入配置（自动 json 序列化）"""
    vj = json.dumps(value, ensure_ascii=False, default=str)
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute('''
            INSERT INTO user_strategy_config(key, value_json, updated_at)
            VALUES (?, ?, NOW())
            ON CONFLICT(key) DO UPDATE SET
                value_json = EXCLUDED.value_json,
                updated_at = NOW()
        ''', (key, vj))
    else:
        cur.execute('''
            INSERT INTO user_strategy_config(key, value_json)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = CURRENT_TIMESTAMP
        ''', (key, vj))
    conn.commit()
    conn.close()
    return True


def reset(key: str) -> bool:
    """删除某 key（下次 get() 回退到默认）"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('DELETE FROM user_strategy_config WHERE key = ?', (key,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def list_all() -> List[Dict[str, Any]]:
    """列所有 key 当前生效值 + 默认值 + 描述"""
    out = []
    for key, meta in DEFAULTS.items():
        out.append({
            'key': key,
            'cn': meta['cn'],
            'type': meta['type'],
            'description': meta['description'],
            'default': meta['value'],
            'current': get(key),
        })
    return out


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== user_strategy_config 自检 ===')
    print(f'默认配置项: {len(DEFAULTS)} 个\n')
    for item in list_all():
        print(f"  {item['cn']:30s} 当前={item['current']:8} 默认={item['default']}")
