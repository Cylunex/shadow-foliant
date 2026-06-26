# -*- coding: utf-8 -*-
"""data.sources._common —— 原子源归一工具(从现有散落处收口)。

这里只放**纯函数 / 薄封装**,不依赖 datahub(sources 不可回调门面,避免循环依赖)。
代码归一、列名/单位归一、HTTP 取数、限流/akshare 兜底封装统一从此处取,
保证各 sources/*.py 直连源逐字段对齐(volume 单位'股'、K线 DatetimeIndex='Date' + 大写 OCHLV)。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import pandas as pd


# ── 代码归一(各家 provider 前缀口径) ───────────────────────────────────────

def norm_code(code: str) -> str:
    """规整 A 股代码为 6 位(去 sh/sz/bj 前缀、补零)。非数字代码原样返回。
    与 datahub._norm_code 同口径。"""
    c = str(code).strip().lower()
    for p in ('sh', 'sz', 'bj'):
        if c.startswith(p):
            c = c[len(p):]
            break
    return c.zfill(6) if c.isdigit() and len(c) <= 6 else str(code).strip()


def em_secid(code: str) -> str:
    """6 位代码 → 东财 secid('1.xxxxxx'=沪 / '0.xxxxxx'=深/京)。与 datahub._em_secid 同口径。
    沪(1):6/68(主板/科创)、5(基金 ETF/LOF)、11/13(债)、900(沪 B)。
    深/京(0):00/30、15/16/12、920/92/8x(北交所)、其余。'9' 有歧义:900→沪 B(1),920→北交(0)。"""
    c = norm_code(code)
    if c[:1] in ('6', '5') or c[:2] in ('11', '13') or c[:3] == '900':
        return f'1.{c}'
    return f'0.{c}'


def sina_code(code: str) -> str:
    """6 位代码 → 新浪带前缀(sh600519 / sz000001 / bj920xxx)。与 datahub._sina_symbol 同口径。"""
    c = norm_code(code)
    if c[:3] in ('920',) or c[0] in ('4', '8'):
        return 'bj' + c
    if c[0] in ('0', '2', '3'):
        return 'sz' + c
    return 'sh' + c   # 6/9/5 开头(沪)及兜底


def tencent_code(code: str) -> str:
    """6 位代码 → 腾讯带前缀(sh/sz/bj)。已带前缀原样返回。"""
    c = str(code).strip().lower()
    import re as _re
    if _re.match(r'^(sh|sz|bj)\d+$', c):
        return c
    c6 = norm_code(code)
    if c6.startswith(('6', '9')):
        return 'sh' + c6
    if c6.startswith('8') or c6[:3] == '920':
        return 'bj' + c6
    return 'sz' + c6


def bs_code(code: str) -> str:
    """6 位代码 → baostock 代码(sh.xxxxxx / sz.xxxxxx)。与 baostock_safe._bs_code 同口径。"""
    c = ''.join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)
    if c[:1] in ('6', '5') or c[:3] == '900' or c[:3] == '688':
        return f'sh.{c}'
    return f'sz.{c}'


# 已知指数代码(与个股 6 位码重码:000001=上证综指 vs 平安银行)。东财走个股 secid 会拿错票,
# 故对这些代码东财直连放弃,交指数专路。与 datahub._EM_INDEX_CODES 同集。
EM_INDEX_CODES = frozenset({
    '000001', '000010', '000016', '000300', '000688', '000852', '000903',
    '000905', '000906',
    '399001', '399005', '399006', '399300', '399905', '399852',
})


# ── K线列名/单位归一 ────────────────────────────────────────────────────

def to_ohlcv(df: pd.DataFrame, *, date_col: Optional[str] = None,
             vol_mult: float = 1.0) -> pd.DataFrame:
    """把任意源的日线归一成项目契约:DatetimeIndex(name='Date') + 大写列 Open/High/Low/Close/Volume。
    · date_col:日期列名(None→若已是 DatetimeIndex 则保留索引)。
    · vol_mult:成交量单位换算系数(东财/akshare '手'→'股' 传 100;本就'股'传 1)。
    列缺失/无法归一 → 返回空 DataFrame(让 _route 跳过)。返回升序、去重、dropna。"""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    # 大小写/中文列名映射到标准大写英文
    alias = {
        'open': 'Open', '开盘': 'Open', 'o': 'Open',
        'high': 'High', '最高': 'High', 'h': 'High',
        'low': 'Low', '最低': 'Low', 'l': 'Low',
        'close': 'Close', '收盘': 'Close', 'c': 'Close',
        'volume': 'Volume', '成交量': 'Volume', 'vol': 'Volume', 'v': 'Volume',
        'date': 'Date', '日期': 'Date', 'datetime': 'Date',
    }
    out = df.copy()
    out.columns = [alias.get(str(c).strip().lower(), str(c)) for c in out.columns]
    try:
        if date_col is not None:
            dcol = alias.get(str(date_col).strip().lower(), date_col)
            out['Date'] = pd.to_datetime(out[dcol], errors='coerce')
            out = out.set_index('Date')
        elif 'Date' in out.columns:
            out['Date'] = pd.to_datetime(out['Date'], errors='coerce')
            out = out.set_index('Date')
        else:
            out.index = pd.to_datetime(out.index, errors='coerce')
            out.index.name = 'Date'
        for c in ('Open', 'High', 'Low', 'Close', 'Volume'):
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors='coerce')
        if 'Volume' in out.columns and vol_mult != 1.0:
            out['Volume'] = out['Volume'] * vol_mult
        keep = [c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c in out.columns]
        if 'Close' not in keep:
            return pd.DataFrame()
        out = out[keep]
        out = out[~out.index.isna()].dropna(subset=['Close'])
        out = out[~out.index.duplicated(keep='last')].sort_index()
        out.index = out.index.normalize()
        out.index.name = 'Date'
        return out
    except Exception:
        return pd.DataFrame()


# ── HTTP / 限流 / akshare 兜底封装(直连源共用) ─────────────────────────────

_DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36')


def http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                  timeout: int = 8, encoding: str = 'utf-8') -> Any:
    """统一 GET → JSON(标准库 urllib,无第三方依赖)。失败抛异常(交 _route 吞)。"""
    import json as _json
    import urllib.request
    h = {'User-Agent': _DEFAULT_UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode(encoding, 'replace')
    return _json.loads(raw)


def http_get_text(url: str, headers: Optional[Dict[str, str]] = None,
                  timeout: int = 8, encoding: str = 'utf-8') -> str:
    """统一 GET → 文本。失败抛异常。"""
    import urllib.request
    h = {'User-Agent': _DEFAULT_UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout).read().decode(encoding, 'replace')


def throttle(source: str = 'default') -> float:
    """按源最小间隔限流(复用 data/rate_limiter)。返回实际 sleep 秒。"""
    try:
        from rate_limiter import throttle as _t
        return _t(source)
    except Exception:
        return 0.0


def ak_safe(fn: Callable[..., Any], *args, timeout: int = 30, **kwargs) -> Any:
    """akshare 超时/异常薄封装(复用 data/akshare_safe.call)。**仅 sources/akshare.py 末位兜底层用**。"""
    from akshare_safe import call as _call
    return _call(fn, *args, timeout=timeout, **kwargs)


if __name__ == '__main__':
    print('=== data.sources._common 自检 ===')
    assert norm_code('sh600519') == '600519'
    assert norm_code('000001') == '000001'
    assert em_secid('600519') == '1.600519'
    assert em_secid('000001') == '0.000001'
    assert em_secid('900901') == '1.900901'   # 沪 B
    assert em_secid('920819') == '0.920819'    # 北交所
    assert sina_code('600519') == 'sh600519'
    assert sina_code('000001') == 'sz000001'
    assert bs_code('688981') == 'sh.688981'
    assert bs_code('300750') == 'sz.300750'
    assert tencent_code('600519') == 'sh600519'
    df = pd.DataFrame({'日期': ['2026-01-02', '2026-01-03'], '开盘': [10, 11],
                       '最高': [12, 12], '最低': [9, 10], '收盘': [11, 11], '成交量': [100, 200]})
    o = to_ohlcv(df, date_col='日期', vol_mult=100)
    assert list(o.columns) == ['Open', 'High', 'Low', 'Close', 'Volume'], o.columns.tolist()
    assert o.index.name == 'Date' and o['Volume'].iloc[0] == 10000
    print('  norm/secid/sina/bs/tencent/to_ohlcv all passed')
    print('OK')
