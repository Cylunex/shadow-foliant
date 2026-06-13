"""零依赖 A股取数兜底 —— 腾讯 + 新浪双源(移植自 Ashare.py,标准化输出)。

定位:data_source_manager 取数链的**最后一环**。当 东财直连/mootdx/akshare/tushare 全失败时,
用腾讯/新浪公开接口兜底(尤其分钟线),保证"几乎不断粮"。仅 requests + pandas,无 key、无额外依赖。

返回统一标准格式:DataFrame[date(datetime), open, high, low, close, volume] 升序。
支持 frequency: 1d/1w/1M + 1m/5m/15m/30m/60m。日/周/月新浪主、腾讯备;1m 仅腾讯。
局限:无成交额/换手率,复权质量一般,历史约 5000 条。
"""

import datetime
import json
from typing import Optional

import pandas as pd
import requests

_TIMEOUT = 12


def _norm_code(code: str) -> str:
    """600519 → sh600519;000001 → sz000001;已带前缀/带 .XSHG/.XSHE 也兼容。"""
    c = str(code).strip()
    if c.endswith('.XSHG'):
        return 'sh' + c[:6]
    if c.endswith('.XSHE'):
        return 'sz' + c[:6]
    if c[:2].lower() in ('sh', 'sz', 'bj'):
        return c.lower()
    if c.isdigit():
        if c.startswith('6'):
            return 'sh' + c
        if c.startswith(('0', '3')):
            return 'sz' + c
        if c.startswith(('8', '4')):
            return 'bj' + c
    return c


def _get_price_day_tx(code, end_date='', count=120, frequency='1d') -> Optional[pd.DataFrame]:
    unit = 'week' if frequency in '1w' else 'month' if frequency in '1M' else 'day'
    if end_date:
        end_date = end_date.strftime('%Y-%m-%d') if isinstance(end_date, datetime.date) else str(end_date).split(' ')[0]
    if end_date == datetime.datetime.now().strftime('%Y-%m-%d'):
        end_date = ''
    url = f'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{unit},,{end_date},{count},qfq'
    st = json.loads(requests.get(url, timeout=_TIMEOUT).content)
    ms = 'qfq' + unit
    stk = st['data'][code]
    buf = stk[ms] if ms in stk else stk[unit]
    df = pd.DataFrame(buf, columns=['time', 'open', 'close', 'high', 'low', 'volume'], dtype='float')
    df['date'] = pd.to_datetime(df['time'])
    return df[['date', 'open', 'high', 'low', 'close', 'volume']]


def _get_price_min_tx(code, count=120, frequency='5m') -> Optional[pd.DataFrame]:
    ts = int(frequency[:-1]) if frequency[:-1].isdigit() else 1
    url = f'http://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m{ts},,{count}'
    st = json.loads(requests.get(url, timeout=_TIMEOUT).content)
    buf = st['data'][code]['m' + str(ts)]
    df = pd.DataFrame(buf, columns=['time', 'open', 'close', 'high', 'low', 'volume', 'n1', 'n2'])
    df = df[['time', 'open', 'close', 'high', 'low', 'volume']].astype(
        {'open': 'float', 'close': 'float', 'high': 'float', 'low': 'float', 'volume': 'float'})
    df['date'] = pd.to_datetime(df['time'])
    return df[['date', 'open', 'high', 'low', 'close', 'volume']]


def _get_price_sina(code, count=120, frequency='240m') -> Optional[pd.DataFrame]:
    scale = {'1d': 240, '1w': 1200, '1M': 7200}.get(frequency)
    if scale is None:
        scale = int(frequency[:-1]) if frequency[:-1].isdigit() else 240
    url = (f'http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           f'CN_MarketData.getKLineData?symbol={code}&scale={scale}&ma=5&datalen={count}')
    arr = json.loads(requests.get(url, timeout=_TIMEOUT).content)
    df = pd.DataFrame(arr, columns=['day', 'open', 'high', 'low', 'close', 'volume'])
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)
    df['date'] = pd.to_datetime(df['day'])
    return df[['date', 'open', 'high', 'low', 'close', 'volume']]


def get_price(code: str, count: int = 120, frequency: str = '1d') -> Optional[pd.DataFrame]:
    """零依赖兜底取价。frequency: 1d/1w/1M/1m/5m/15m/30m/60m。失败返回 None(不抛)。
    返回标准 DataFrame[date,open,high,low,close,volume] 升序。"""
    xcode = _norm_code(code)
    try:
        if frequency in ('1d', '1w', '1M'):
            try:
                return _get_price_sina(xcode, count, frequency).sort_values('date').reset_index(drop=True)
            except Exception:
                return _get_price_day_tx(xcode, count=count, frequency=frequency).sort_values('date').reset_index(drop=True)
        if frequency in ('1m', '5m', '15m', '30m', '60m'):
            if frequency == '1m':
                return _get_price_min_tx(xcode, count, frequency).sort_values('date').reset_index(drop=True)
            try:
                return _get_price_sina(xcode, count, frequency).sort_values('date').reset_index(drop=True)
            except Exception:
                return _get_price_min_tx(xcode, count, frequency).sort_values('date').reset_index(drop=True)
    except Exception as e:
        print(f'[ashare_fallback] get_price({code},{frequency}) 失败: {e}')
    return None


def available() -> bool:
    return True  # 零依赖,只要能联网就可用


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    for code, freq in (('600519', '1d'), ('000001', '5m')):
        d = get_price(code, 5, freq)
        print(f'{code} {freq}:\n', d.tail(2).to_string() if d is not None else None)
