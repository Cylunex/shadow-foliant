"""FRED 美国宏观经济数据接入

借鉴 FinceptTerminal/scripts/fred_data.py，精简为 A 股最关心的 12 个核心序列。
设计原则：
  - 优先 FRED API（需环境变量 FRED_API_KEY，免费申请：https://fred.stlouisfed.org/docs/api/api_key.html）
  - 无 API key 时 fallback 到 yfinance（少数关键指标如 10Y 国债、VIX、美元指数有等价 ticker）
  - 不持久化，按需拉取 + 内存 lru_cache（凌晨任务一天拉一次即可）

12 个序列（A 股相关性排序）：
  利率：FEDFUNDS / DGS10 / DGS2
  通胀：CPIAUCSL / PCEPI
  就业：UNRATE / PAYEMS
  增长：GDP / INDPRO
  货币：DTWEXBGS / M2SL
  风险：VIXCLS / T10Y2Y
"""

import os
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Any

import requests

FRED_API_BASE = 'https://api.stlouisfed.org/fred'

# 序列定义：fred_id -> (中文名, yfinance fallback ticker 或 None)
CORE_SERIES = {
    'FEDFUNDS':  ('美联储基金利率(月度)', None),
    'DGS10':     ('美10年期国债收益率', '^TNX'),
    'DGS2':      ('美2年期国债收益率', None),
    'T10Y2Y':    ('10Y-2Y 收益率曲线(衰退指标)', None),
    'CPIAUCSL':  ('美CPI 综合指数', None),
    'PCEPI':     ('美PCE 通胀指数', None),
    'UNRATE':    ('美失业率', None),
    'PAYEMS':    ('美非农就业', None),
    'GDP':       ('美GDP(季度)', None),
    'INDPRO':    ('美工业生产指数', None),
    'DTWEXBGS':  ('美元指数(贸易加权)', 'DX-Y.NYB'),
    'M2SL':      ('美M2 货币供应', None),
    'VIXCLS':    ('VIX 恐慌指数', '^VIX'),
}


def _api_key() -> str:
    return os.getenv('FRED_API_KEY', '').strip()


@lru_cache(maxsize=128)
def _fetch_fred_series(series_id: str, days: int = 365) -> List[Dict[str, Any]]:
    """直接调 FRED API 拉序列；无 key 返回 []。结果按日期降序"""
    key = _api_key()
    if not key:
        return []
    start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        r = requests.get(
            f'{FRED_API_BASE}/series/observations',
            params={
                'series_id': series_id,
                'api_key': key,
                'file_type': 'json',
                'observation_start': start,
                'sort_order': 'desc',
                'limit': 500,
            },
            timeout=15,
        )
        r.raise_for_status()
        obs = r.json().get('observations', [])
        out = []
        for o in obs:
            v = o.get('value')
            if v in (None, '.', ''):
                continue
            try:
                out.append({'date': o['date'], 'value': float(v)})
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f'[FRED] {series_id} 拉取失败: {e}')
        return []


def _fetch_yfinance_fallback(ticker: str, days: int = 90) -> List[Dict[str, Any]]:
    """无 FRED key 时用 Yahoo Finance V8 API（比 yfinance 库稳定）"""
    import requests
    headers = {'User-Agent': 'Mozilla/5.0'}
    import config as _cfg
    proxies = _cfg.PROXIES.copy()
    if not proxies.get('http') and not proxies.get('https'):
        proxies = None
    try:
        # 映射 ticker 到 Yahoo V8 格式
        v8_map = {'^TNX': '%5ETNX', '^VIX': '%5EVIX', 'DX-Y.NYB': 'DX-Y.NYB'}
        sym = v8_map.get(ticker, ticker)
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={min(days, 60)}d&interval=1d'
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=10)
        if resp.status_code != 200:
            print(f'[FRED-fallback] Yahoo V8 {ticker} HTTP {resp.status_code}')
            return []
        data = resp.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        closes = result['indicators']['quote'][0]['close']
        out = []
        for i in range(len(timestamps)):
            if closes[i] is not None:
                d = datetime.fromtimestamp(timestamps[i]).strftime('%Y-%m-%d')
                out.append({'date': d, 'value': float(closes[i])})
        return out[::-1]  # 最新在前
    except Exception as e:
        print(f'[FRED-fallback] Yahoo V8 {ticker} 失败: {e}')
        return []


def get_series(series_id: str, days: int = 365) -> List[Dict[str, Any]]:
    """拉单个序列，优先 FRED，失败/无 key 时 fallback yfinance"""
    if series_id not in CORE_SERIES:
        return _fetch_fred_series(series_id, days)
    cn_name, yf_ticker = CORE_SERIES[series_id]
    data = _fetch_fred_series(series_id, days)
    if data:
        return data
    if yf_ticker:
        return _fetch_yfinance_fallback(yf_ticker, min(days, 365))
    return []


def get_fed_snapshot() -> Dict[str, Dict[str, Any]]:
    """一键拉 12 个核心序列的最新值 + 周/月变动

    返回:
      {series_id: {name, latest, latest_date, prev_value, change_pct,
                   source ('fred'|'yfinance'|'none')}}
    """
    snapshot = {}
    fred_ok = bool(_api_key())
    for sid, (cn_name, yf_ticker) in CORE_SERIES.items():
        data = _fetch_fred_series(sid, days=60) if fred_ok else []
        source = 'fred' if data else ''
        if not data and yf_ticker:
            data = _fetch_yfinance_fallback(yf_ticker, days=60)
            source = 'yfinance' if data else 'none'
        elif not data:
            source = 'none'

        if data:
            latest = data[0]
            prev_idx = min(5, len(data) - 1)
            prev = data[prev_idx] if prev_idx > 0 else None
            change_pct = None
            if prev and prev['value'] not in (0, None):
                change_pct = (latest['value'] - prev['value']) / abs(prev['value']) * 100
            snapshot[sid] = {
                'name': cn_name,
                'latest': latest['value'],
                'latest_date': latest['date'],
                'prev_value': prev['value'] if prev else None,
                'change_pct': round(change_pct, 2) if change_pct is not None else None,
                'source': source,
            }
        else:
            snapshot[sid] = {
                'name': cn_name,
                'latest': None, 'latest_date': None,
                'prev_value': None, 'change_pct': None,
                'source': source,
            }
    return snapshot


def format_snapshot(snapshot: Dict[str, Dict[str, Any]]) -> str:
    """格式化为可塞 prompt 的多行文本"""
    if not snapshot:
        return '（无数据）'
    lines = []
    for sid, info in snapshot.items():
        if info['latest'] is None:
            lines.append(f"  {info['name']} [{sid}]: 无数据")
            continue
        chg = info['change_pct']
        chg_str = f" (5周期变 {chg:+.2f}%)" if chg is not None else ''
        lines.append(
            f"  {info['name']} [{sid}]: {info['latest']:.2f} "
            f"@ {info['latest_date']}{chg_str}  [{info['source']}]"
        )
    return '\n'.join(lines)


if __name__ == '__main__':
    print('=== FRED 自检 ===')
    print(f'FRED_API_KEY 已配置: {bool(_api_key())}')
    snap = get_fed_snapshot()
    print(f'\n核心序列 ({len(snap)} 项):')
    print(format_snapshot(snap))
