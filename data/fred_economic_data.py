"""FRED 美国宏观经济数据接入

借鉴 FinceptTerminal/scripts/fred_data.py，精简为 A 股最关心的 12 个核心序列。
设计原则：
  - 优先 FRED API（需环境变量 FRED_API_KEY，免费申请：https://fred.stlouisfed.org/docs/api/api_key.html）
  - 关键日线宏观指标优先用 FRED 公共导出补齐，随后用可用的市场代理指标
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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from zipfile import BadZipFile
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
                   source ('fred'|'fred_public'|'yfinance'|'none')}}
    """
    snapshot = {}
    fred_ok = bool(_api_key())
    daily_ids = ('DGS10', 'DTWEXBGS', 'VIXCLS')
    api_data = {sid: _fetch_fred_series(sid, days=60) for sid in CORE_SERIES} if fred_ok else {}
    daily_missing = [sid for sid in daily_ids if not api_data.get(sid)]
    try:
        ny_day = datetime.now(ZoneInfo('America/New_York')).date()
        public_data = _public_daily(daily_missing, ny_day, days=14) if daily_missing else {}
    except (ValueError, OSError, BadZipFile, requests.RequestException) as exc:
        print(f'[FRED] public daily unavailable: {type(exc).__name__}')
        public_data = {}
    for sid, (cn_name, yf_ticker) in CORE_SERIES.items():
        data = api_data.get(sid) or public_data.get(sid) or []
        source = 'fred' if api_data.get(sid) else 'fred_public' if data else ''
        if not data and yf_ticker and sid == 'DTWEXBGS':
            # The ICE DXY composition differs from the Fed's broad dollar index.
            cn_name = '美元指数（DXY）'
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
    if not any(info.get('latest') is not None for info in snapshot.values()):
        return '（无数据）'
    lines, missing = [], []
    for sid, info in snapshot.items():
        if info['latest'] is None:
            missing.append(sid)
            continue
        chg = info['change_pct']
        chg_str = f" (5周期变 {chg:+.2f}%)" if chg is not None else ''
        lines.append(
            f"  {info['name']} [{sid}]: {info['latest']:.2f} "
            f"@ {info['latest_date']}{chg_str}  [{info['source']}]"
        )
    if missing:
        lines.append('  缺项：' + '、'.join(missing))
    return '\n'.join(lines)


def _public_daily(series, as_of, *, days=10):
    import csv
    import io
    import math
    from zipfile import ZipFile, is_zipfile

    response = requests.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": ",".join(series), "cosd": (as_of - timedelta(days=days)).isoformat(),
                "coed": as_of.isoformat()},
        timeout=(3, 6),
    )
    response.raise_for_status()
    observations = {series_id: {} for series_id in series}
    archive = io.BytesIO(response.content)
    if is_zipfile(archive):
        # FRED splits series with differing frequency labels into CSV members.
        with ZipFile(archive) as files:
            tables = [files.read(member).decode('utf-8-sig').splitlines() for member in files.infolist()
                      if member.filename.endswith('.csv') and member.file_size <= 262144]
    else:
        tables = [response.text.splitlines()]
    for table in tables:
        for row in csv.DictReader(table):
            try:
                day = date.fromisoformat(row.get("observation_date") or row.get("DATE") or "")
            except ValueError:
                continue
            if not as_of - timedelta(days=days) <= day <= as_of:
                continue
            for key in series:
                try:
                    value = float(row.get(key) or "")
                except ValueError:
                    continue
                if math.isfinite(value):
                    observations[key][day] = value

    return {key: [{'date': day.isoformat(), 'value': value}
                  for day, value in sorted(values.items(), reverse=True)]
            for key, values in observations.items()}


def _index_line(name, rows, as_of):
    import math

    valid = [(row['date'], row['value']) for row in rows
             if math.isfinite(row['value']) and row['value'] > 0]
    if len(valid) < 2:
        return ''
    day, current = valid[0]
    if not as_of - timedelta(days=4) <= date.fromisoformat(day) <= as_of:
        return ''
    previous = valid[1][1]
    return f"{name} {day}: {current:.2f} ({(current / previous - 1) * 100:+.2f}%)"


def us_index_closes(as_of):
    """Read the public FRED daily-close export as a bounded fallback."""
    series = {"DJIA": "道琼斯", "SP500": "标普500", "NASDAQCOM": "纳斯达克"}
    observations = _public_daily(series, as_of)
    return {name: line for key, name in series.items()
            if (line := _index_line(name, observations[key], as_of))}


def eastmoney_sp500_close(as_of):
    """Fetch dated S&P 500 index bars from the Eastmoney global-index history."""
    response = requests.get(
        'https://push2his.eastmoney.com/api/qt/stock/kline/get',
        params={'secid': '100.SPX', 'klt': 101, 'fqt': 0, 'lmt': 8,
                'end': as_of.strftime('%Y%m%d'), 'fields1': 'f1,f2,f3,f4,f5,f6',
                'fields2': 'f51,f52,f53', 'iscca': 1, 'forcect': 1},
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'},
        timeout=(3, 5),
    )
    response.raise_for_status()
    data = response.json().get('data') or {}
    if data.get('code') != 'SPX':
        return ''
    bars = {}
    for line in data.get('klines') or []:
        try:
            day, _, close = line.split(',')
            if date.fromisoformat(day) <= as_of:
                bars[day] = float(close)
        except ValueError:
            continue
    return _index_line('标普500', [{'date': day, 'value': value}
                                  for day, value in sorted(bars.items(), reverse=True)], as_of)


if __name__ == '__main__':
    print('=== FRED 自检 ===')
    print(f'FRED_API_KEY 已配置: {bool(_api_key())}')
    snap = get_fed_snapshot()
    print(f'\n核心序列 ({len(snap)} 项):')
    print(format_snapshot(snap))
