import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""北向资金本地自缓存

背景：eastmoney 全系北向数据自 2024-08 起断供（净买额字段返回 0/NaN，
adata.sentiment.north.north_flow() 因此也拿不到真实值），导致历史窗口形同虚设。
本模块直连同花顺 hsgtApi 拉取真实分钟级数据，取收盘累计入库，
按日累积形成历史，供 AI 分析师与定时任务使用。

数据源优先级：
  1. 同花顺 hsgtApi（零鉴权，分钟级，取尾部为当日收盘累计）
  2. adata（断供占位，仅作 backfill 用）

持久化：
  - PG 模式：northbound_flow_daily 表（init_postgres.sql 已建）
  - SQLite 模式：本模块导入时自动建表

接口：
  - refresh_today()       拉同花顺当日数据入库（jobs_hub 每日 15:40 调）
  - get_recent(days=30)   读最近 N 个交易日
  - backfill_from_adata() 一次性回灌（断供期间值为 0）
"""

import os
from datetime import datetime
from typing import List, Dict, Optional

from db_compat import connect as db_connect, USE_POSTGRES

_CACHE_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')

_HSGT_URL = 'https://data.hexin.cn/market/hsgtApi/method/dayChart/'
_HSGT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'Chrome/117.0.0.0 Safari/537.36'
    ),
    'Host': 'data.hexin.cn',
    'Referer': 'https://data.hexin.cn/',
}


def _init_table():
    """SQLite 模式自动建表；PG 模式靠 init_postgres.sql"""
    if USE_POSTGRES:
        return
    conn = db_connect(_CACHE_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS northbound_flow_daily (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date  TEXT NOT NULL UNIQUE,
            hgt_yi      REAL,
            sgt_yi      REAL,
            net_total   REAL,
            source      TEXT DEFAULT 'hexin',
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


_init_table()


def fetch_realtime_hexin(timeout: int = 10) -> Optional[Dict]:
    """拉同花顺当日北向分钟级数据，取最后一个非空点作为收盘累计

    返回 dict: {'date', 'hgt_yi', 'sgt_yi', 'last_time'} 或 None
    """
    import requests
    try:
        r = requests.get(_HSGT_URL, headers=_HSGT_HEADERS, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        times = d.get('time', [])
        hgt = d.get('hgt', [])
        sgt = d.get('sgt', [])
        if not times or not hgt or not sgt:
            return None
        max_i = min(len(times), len(hgt), len(sgt)) - 1
        last_idx = None
        for i in range(max_i, -1, -1):
            if hgt[i] is not None and sgt[i] is not None:
                last_idx = i
                break
        if last_idx is None:
            return None
        return {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'hgt_yi': float(hgt[last_idx]),
            'sgt_yi': float(sgt[last_idx]),
            'last_time': str(times[last_idx]),
        }
    except Exception as e:
        print(f'[northbound_cache] hexin 拉取失败: {e}')
        return None


def upsert(trade_date: str, hgt_yi: float, sgt_yi: float, source: str = 'hexin'):
    """幂等写入某交易日北向数据"""
    net_total = (hgt_yi or 0) + (sgt_yi or 0)
    conn = db_connect(_CACHE_DB_PATH)
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute('''
            INSERT INTO northbound_flow_daily(trade_date, hgt_yi, sgt_yi, net_total, source, updated_at)
            VALUES (?, ?, ?, ?, ?, NOW())
            ON CONFLICT(trade_date) DO UPDATE SET
                hgt_yi = EXCLUDED.hgt_yi,
                sgt_yi = EXCLUDED.sgt_yi,
                net_total = EXCLUDED.net_total,
                source = EXCLUDED.source,
                updated_at = NOW()
        ''', (trade_date, hgt_yi, sgt_yi, net_total, source))
    else:
        cur.execute('''
            INSERT INTO northbound_flow_daily(trade_date, hgt_yi, sgt_yi, net_total, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                hgt_yi = excluded.hgt_yi,
                sgt_yi = excluded.sgt_yi,
                net_total = excluded.net_total,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
        ''', (trade_date, hgt_yi, sgt_yi, net_total, source))
    conn.commit()
    conn.close()


def refresh_today() -> Optional[Dict]:
    """拉取当日实时数据并入库，返回入库记录或 None。
    
    防重复：若数值与最近一条缓存完全一致（同花顺断供期间返回固定值），跳过入库。
    """
    data = fetch_realtime_hexin()
    if not data:
        return None
    # 检查是否与最近缓存值完全相同（数据源断供时 hexin 返回固定死值）
    recent = get_recent(1)
    if recent:
        last = recent[0]
        if (abs(last['hgt_yi'] - data['hgt_yi']) < 0.01 and 
            abs(last['sgt_yi'] - data['sgt_yi']) < 0.01):
            print(f"[northbound_cache] 数据与上次({last['trade_date']})一致，疑似来源断供，跳过")
            return None
    upsert(data['date'], data['hgt_yi'], data['sgt_yi'], 'hexin')
    return data


def get_recent(days: int = 30) -> List[Dict]:
    """读取最近 N 个交易日北向数据（按日期降序）

    返回 list[dict]，每条字段：
      新字段：trade_date(YYYY-MM-DD), hgt_yi, sgt_yi, net_total（亿元），source
      兼容字段：net_hgt, net_sgt（元，给旧调用方用），net_tgt（恒 0）
    """
    conn = db_connect(_CACHE_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT trade_date, hgt_yi, sgt_yi, net_total, source
        FROM northbound_flow_daily
        ORDER BY trade_date DESC
        LIMIT ?
    ''', (days,))
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        td, hgt, sgt, net_total, source = r
        td_str = td.strftime('%Y-%m-%d') if hasattr(td, 'strftime') else str(td)
        out.append({
            'trade_date': td_str,
            'hgt_yi': hgt,
            'sgt_yi': sgt,
            'net_total': net_total,
            'source': source,
            'net_hgt': (hgt or 0) * 1e8,
            'net_sgt': (sgt or 0) * 1e8,
            'net_tgt': 0,
        })
    return out


def backfill_from_adata() -> int:
    """一次性从 adata 拉历史灌入（断供期间值为 0，仅占位）"""
    try:
        import adata
        df = adata.sentiment.north.north_flow()
        if df is None or df.empty:
            return 0
        n = 0
        for _, row in df.iterrows():
            td = str(row.get('trade_date', ''))
            if not td:
                continue
            hgt = float(row.get('net_hgt', 0) or 0) / 1e8
            sgt = float(row.get('net_sgt', 0) or 0) / 1e8
            upsert(td, hgt, sgt, 'adata')
            n += 1
        return n
    except Exception as e:
        print(f'[northbound_cache] backfill 失败: {e}')
        return 0


if __name__ == '__main__':
    print('=== northbound_cache 自检 ===')
    print(f'USE_POSTGRES={USE_POSTGRES}')
    print('\n1. 拉取同花顺实时数据并入库...')
    data = refresh_today()
    print(f'   入库: {data}')
    print('\n2. 读取最近 5 个交易日...')
    rows = get_recent(5)
    for r in rows:
        print(f"   {r['trade_date']}: 沪{r['hgt_yi']:.2f}亿 / "
              f"深{r['sgt_yi']:.2f}亿 / 合计{r['net_total']:.2f}亿  [{r['source']}]")
    print('\nOK')
