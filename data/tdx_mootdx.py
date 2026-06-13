"""通达信(TDX)数据源 —— 基于 mootdx 的纯 Python 实现,可移植(不依赖内网 Go 服务)。

为什么用它:原 monitor/smart_monitor_tdx_data.py 写死内网 http://192.168.x.x:8181 的 Go 服务,
OpenClaw/异地部署没有该服务。mootdx 直连公网通达信行情服务器(端口7709),纯 Python、无券商、无 key。

⚠️ 关键经验(实测):mootdx 的 `bestip` 自动选服务器在本网络不可靠(会选到不通的服务器报错),
因此本模块**内置候选服务器列表 + socket 探测第一个可连的**,而不用 bestip。可用 env `TDX_SERVERS`
覆盖(形如 "ip:port,ip:port")。

返回统一标准格式:DataFrame[date(datetime), open, high, low, close, volume, amount] 升序。
取不到/不可用一律返回 None(由上层 data_source_manager 继续降级)。

依赖:pip install mootdx  (注:mootdx 声明 httpx<0.26,但实测 httpx 0.28 下正常工作;
mcp 需 httpx>=0.27.1,故保持 httpx>=0.27.1,忽略 mootdx 的保守 pin 警告)
"""

import os
import socket
import threading
from typing import Optional

import pandas as pd

# 候选公网通达信行情服务器(实测可连优先;mootdx bestip 不可靠故内置)
_DEFAULT_SERVERS = [
    ('123.125.108.14', 7709),
    ('124.71.187.122', 7709),
    ('110.41.147.114', 7709),
    ('119.147.212.81', 7709),
    ('218.108.98.244', 7709),
    ('124.74.236.94', 7709),
    ('60.12.136.250', 7709),
]

# mootdx frequency 取值:8=1m 0=5m 1=15m 2=30m 3=1h 9=日 5=周 6=月
_FREQ_MAP = {
    '1m': 8, '5m': 0, '15m': 1, '30m': 2, '1h': 3, '60m': 3,
    'day': 9, 'd': 9, '1d': 9, 'week': 5, 'w': 5, 'month': 6, 'm': 6,
}

_lock = threading.Lock()
_good_server = None          # 探测到的可连服务器,进程内缓存
_probed = False
_unavailable = False         # mootdx 未安装等硬性不可用


def _candidate_servers():
    env = os.getenv('TDX_SERVERS', '').strip()
    if env:
        out = []
        for part in env.split(','):
            if ':' in part:
                ip, port = part.strip().split(':')
                out.append((ip.strip(), int(port)))
        if out:
            return out
    return _DEFAULT_SERVERS


def _probe_server(timeout=2.0):
    """socket 探测候选里第一个可连的服务器(进程内缓存)。"""
    global _good_server, _probed
    if _good_server is not None:
        return _good_server
    with _lock:
        if _good_server is not None:
            return _good_server
        for ip, port in _candidate_servers():
            s = socket.socket()
            s.settimeout(timeout)
            try:
                s.connect((ip, port))
                _good_server = (ip, port)
                break
            except Exception:
                continue
            finally:
                s.close()
        _probed = True
    return _good_server


def _client():
    """构造连到可连服务器的 mootdx 标准行情客户端;不可用返回 None。"""
    global _unavailable
    if _unavailable:
        return None
    srv = _probe_server()
    if srv is None:
        return None
    try:
        from mootdx.quotes import Quotes
        return Quotes.factory(market='std', server=srv, timeout=10)
    except ImportError:
        _unavailable = True  # 没装 mootdx,别反复尝试
        print('[tdx_mootdx] 未安装 mootdx,跳过(pip install mootdx)')
        return None
    except Exception as e:
        print(f'[tdx_mootdx] 客户端构造失败: {e}')
        return None


def available() -> bool:
    """是否可用(已安装 mootdx 且有可连服务器)。"""
    return _client() is not None


def _standardize(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or len(df) == 0:
        return None
    if 'datetime' in df.columns:
        df = df.rename(columns={'datetime': 'date'})
    if 'volume' not in df.columns and 'vol' in df.columns:
        df = df.rename(columns={'vol': 'volume'})
    df = df.loc[:, ~df.columns.duplicated()]          # mootdx 可能同时有 vol/volume,去重
    if 'date' not in df.columns:
        df = df.reset_index().rename(columns={df.index.name or 'index': 'date'})
    df['date'] = pd.to_datetime(df['date'])
    keep = [c for c in ['date', 'open', 'high', 'low', 'close', 'volume', 'amount'] if c in df.columns]
    return df[keep].sort_values('date').reset_index(drop=True)


def get_kline(symbol: str, frequency='day', count: int = 800,
              adjust: str = '') -> Optional[pd.DataFrame]:
    """K线。frequency: 1m/5m/15m/30m/1h/day/week/month;adjust: ''/qfq/hfq。
    返回标准(不复权)DataFrame 或 None。

    ⚠️ 复权(adjust=qfq/hfq)本源不支持,一律按不复权 raw 返回:
      ① mootdx 自带 reversion.py 在 pandas≥3 下崩(用了已移除的 `fillna(method=)`);
      ② 其因子源 mootdx.utils.factor.fq_factor 的磁盘缓存只按 symbol 取键、忽略 method,
         hfq 会拿到上次 qfq 的缓存(本源 bug),不可信。
    复权日线请走数据链上游的 东财/akshare/Ashare(data_source_manager 已优先这些)。
    mootdx 在链中只承担「公网不复权行情兜底」一职。"""
    c = _client()
    if c is None:
        return None
    freq = _FREQ_MAP.get(str(frequency), 9)
    off = min(int(count), 800)
    if adjust in ('qfq', 'hfq'):
        print(f'[tdx_mootdx] {symbol} 不支持复权({adjust}),返回不复权;复权请用东财/akshare/Ashare')
    try:
        return _standardize(c.bars(symbol=symbol, frequency=freq, offset=off))
    except Exception as e:
        print(f'[tdx_mootdx] get_kline({symbol}) 失败: {e}')
        return None


def get_minute(symbol: str, date: Optional[str] = None) -> Optional[pd.DataFrame]:
    """当日/历史分时(date=YYYYMMDD 取历史)。"""
    c = _client()
    if c is None:
        return None
    try:
        df = c.minutes(symbol=symbol, date=date) if date else c.minute(symbol=symbol)
        return df if (df is not None and len(df)) else None
    except Exception as e:
        print(f'[tdx_mootdx] get_minute({symbol}) 失败: {e}')
        return None


def get_quote(symbol: str) -> Optional[dict]:
    """实时五档报价(返回 mootdx 原始行,失败 None)。"""
    c = _client()
    if c is None:
        return None
    try:
        df = c.quotes(symbol=[symbol] if isinstance(symbol, str) else symbol)
        if df is not None and len(df):
            return df.iloc[0].to_dict()
    except Exception as e:
        print(f'[tdx_mootdx] get_quote({symbol}) 失败: {e}')
    return None


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('探测服务器:', _probe_server(), '| available:', available())
    d = get_kline('600519', 'day', 5)   # mootdx 只出不复权行情
    print('日K(后2行):\n', d.tail(2).to_string() if d is not None else None)
