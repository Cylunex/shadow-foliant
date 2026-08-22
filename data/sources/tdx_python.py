"""可验证的 TDX 主适配器，基于 tdx-python 的当前协议实现。

第三方 Go 核心会把所选节点写到原生 stdout。为避免生产节点进入主进程日志，本模块把 SDK
隔离在一个持久子进程中，并将该子进程 stdout/stderr 丢弃；主进程只通过 Pipe 收发结构化、
不含错误正文的数据。跨源路由与业务缓存仍由 DataHub 负责。
"""

from __future__ import annotations

import atexit
import multiprocessing as mp
import os
import threading
from typing import Dict, List, Optional

import pandas as pd


_CATEGORIES = {
    '1m': 7, '1min': 7,
    '5m': 0, '5min': 0,
    '15m': 1, '15min': 1,
    '30m': 2, '30min': 2,
    '60m': 3, '60min': 3, '1h': 3,
    '1d': 9, 'day': 9, 'daily': 9, 'd': 9, '101': 9,
    '1w': 5, 'week': 5, 'weekly': 5, 'w': 5,
    '1mo': 6, 'month': 6, 'monthly': 6,
}
_INTRADAY = {0, 1, 2, 3, 7}

_lock = threading.RLock()
_process = None
_pipe = None
_hard_unavailable = False


def _enabled() -> bool:
    return os.getenv('TDX_USE_TDX_PYTHON', 'true').lower() not in {'0', 'false', 'no'}


def _positive_int(value, default: int, minimum: int = 1) -> int:
    try:
        return max(int(value), minimum)
    except (TypeError, ValueError):
        return default


def _positive_float(value, default: float, minimum: float = 1.0) -> float:
    try:
        return max(float(value), minimum)
    except (TypeError, ValueError):
        return default


def available() -> bool:
    """只检查依赖与开关，不连接行情节点。"""
    if not _enabled():
        return False
    try:
        from tdx_py import api  # noqa: F401
        return True
    except Exception:
        return False


def _sdk_symbol(symbol: str) -> str:
    raw = str(symbol).lower().strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())[-6:]
    if len(digits) != 6:
        return ''
    if raw.startswith(('sh', 'sz', 'bj')):
        return raw[:2] + digits
    if digits.startswith(('4', '8', '92')):
        prefix = 'bj'
    elif digits.startswith(('0', '2', '3')):
        prefix = 'sz'
    else:
        prefix = 'sh'
    return prefix + digits


def _worker_main(conn, address: str, timeout_ms: int) -> None:
    """子进程入口：原生 SDK 的节点/连接日志永不进入应用日志。"""
    null_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null_fd, 1)
    os.dup2(null_fd, 2)
    client = None
    try:
        from tdx_py import api
        client = api.dial(address, timeout_ms, True)
        conn.send({'ok': True, 'kind': 'ready'})
    except Exception as exc:
        conn.send({'ok': False, 'kind': 'ready', 'error_type': type(exc).__name__})
        conn.close()
        return

    try:
        while True:
            try:
                request = conn.recv()
            except EOFError:
                break
            action = request.get('action')
            if action == 'stop':
                break
            try:
                if action == 'kline':
                    wanted = int(request['count'])
                    rows = []
                    for start in range(0, wanted, 800):
                        size = min(800, wanted - start)
                        page = client.get_kline(
                            int(request['category']), request['symbol'], start, size
                        )
                        chunk = list(page.list)
                        for item in chunk:
                            rows.append({
                                'time': item.time, 'open': item.open, 'high': item.high,
                                'low': item.low, 'close': item.close,
                                'volume': item.volume, 'amount': item.amount,
                            })
                        if len(chunk) < size:
                            break
                    conn.send({'ok': True, 'rows': rows})
                elif action == 'quotes':
                    quotes = client.get_quote(request['symbols'])
                    rows = []
                    for quote in quotes:
                        bar = quote.kline
                        rows.append({
                            'code': quote.code, 'last': bar.last, 'open': bar.open,
                            'high': bar.high, 'low': bar.low, 'close': bar.close,
                            'volume': bar.volume, 'amount': bar.amount,
                        })
                    conn.send({'ok': True, 'rows': rows})
                else:
                    conn.send({'ok': False, 'error_type': 'UnsupportedAction'})
            except Exception as exc:
                conn.send({'ok': False, 'error_type': type(exc).__name__})
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        conn.close()
        os.close(null_fd)


def _drop_worker() -> None:
    global _process, _pipe
    process, pipe = _process, _pipe
    _process = None
    _pipe = None
    if pipe is not None:
        try:
            pipe.send({'action': 'stop'})
        except Exception:
            pass
        try:
            pipe.close()
        except Exception:
            pass
    if process is not None:
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)


def _ensure_worker() -> bool:
    global _process, _pipe, _hard_unavailable
    if _hard_unavailable or not _enabled():
        return False
    if _process is not None and _process.is_alive() and _pipe is not None:
        return True
    _drop_worker()
    try:
        ctx = mp.get_context('spawn')
        parent, child = ctx.Pipe()
        process = ctx.Process(
            target=_worker_main,
            args=(child, os.getenv('TDX_PYTHON_ADDRESS', '').strip(),
                  _positive_int(os.getenv('TDX_PYTHON_TIMEOUT_MS'), 5000, 500)),
            name='foliant-tdx', daemon=True,
        )
        process.start()
        child.close()
        # 先登记句柄，后续任一步失败都能由统一清理逻辑回收本地进程与 Pipe。
        _process, _pipe = process, parent
        wait_sec = _positive_float(os.getenv('TDX_PYTHON_START_TIMEOUT'), 8.0)
        if not parent.poll(wait_sec):
            _drop_worker()
            return False
        ready = parent.recv()
        if not ready.get('ok'):
            error_type = ready.get('error_type')
            _drop_worker()
            if error_type in {'ImportError', 'ModuleNotFoundError', 'OSError'}:
                _hard_unavailable = True
            return False
        return True
    except Exception:
        _drop_worker()
        return False


def _request(payload: dict) -> Optional[dict]:
    with _lock:
        if not _ensure_worker():
            return None
        try:
            _pipe.send(payload)
            wait_sec = _positive_float(os.getenv('TDX_PYTHON_REQUEST_TIMEOUT'), 10.0)
            if not _pipe.poll(wait_sec):
                _drop_worker()
                return None
            response = _pipe.recv()
            if not response.get('ok'):
                _drop_worker()
                return None
            return response
        except Exception:
            _drop_worker()
            return None


def get_kline(symbol: str, frequency: str = 'day', count: int = 800) -> pd.DataFrame:
    category = _CATEGORIES.get(str(frequency).strip().lower())
    code = _sdk_symbol(symbol)
    if category is None or not code:
        return pd.DataFrame()
    max_bars = _positive_int(os.getenv('MARKET_DATA_MAX_BARS'), 3200)
    wanted = min(_positive_int(count, 800), max_bars)
    response = _request({
        'action': 'kline', 'symbol': code, 'category': category, 'count': wanted,
    })
    rows = response.get('rows', []) if response else []
    if not rows:
        return pd.DataFrame()
    try:
        raw = pd.DataFrame(rows)
        out = pd.DataFrame({
            'date': pd.to_datetime(raw['time'], errors='coerce'),
            'open': pd.to_numeric(raw['open'], errors='coerce') / 1000,
            'high': pd.to_numeric(raw['high'], errors='coerce') / 1000,
            'low': pd.to_numeric(raw['low'], errors='coerce') / 1000,
            'close': pd.to_numeric(raw['close'], errors='coerce') / 1000,
            # tdx-python 实测日线与分钟线 volume 均为“手”。
            'volume': pd.to_numeric(raw['volume'], errors='coerce') * 100,
            'amount': pd.to_numeric(raw['amount'], errors='coerce'),
        })
        if category not in _INTRADAY:
            out['date'] = out['date'].dt.normalize()
        return (out.dropna(subset=['date']).drop_duplicates(subset=['date'], keep='last')
                .sort_values('date').tail(wanted).reset_index(drop=True))
    except Exception:
        return pd.DataFrame()


def _quote_dict(row: dict) -> dict:
    price = float(row.get('close') or 0) / 1000
    last = float(row.get('last') or 0) / 1000
    high = float(row.get('high') or 0) / 1000
    low = float(row.get('low') or 0) / 1000
    change = price - last
    return {
        'name': '', 'price': price, 'last_close': last,
        'open': float(row.get('open') or 0) / 1000, 'high': high, 'low': low,
        'change_amt': round(change, 4),
        'change_pct': round(change / last * 100, 4) if last else 0.0,
        'amount_wan': float(row.get('amount') or 0) / 1e4,
        'turnover_pct': 0.0, 'pe_ttm': 0.0,
        'amplitude_pct': round((high - low) / last * 100, 4) if last else 0.0,
        'mcap_yi': 0.0, 'float_mcap_yi': 0.0, 'pb': 0.0,
        'limit_up': 0.0, 'limit_down': 0.0, 'vol_ratio': 0.0, 'pe_static': 0.0,
    }


def get_quotes(symbols: List[str]) -> Dict[str, dict]:
    pairs = [(str(symbol), _sdk_symbol(symbol)) for symbol in symbols]
    pairs = [(original, code) for original, code in pairs if code]
    if not pairs:
        return {}
    out = {}
    # 通达信报价协议单包容量有限；固定小批量避免大股票池请求整批失败。
    codes = [code for _, code in pairs]
    for pos in range(0, len(codes), 80):
        response = _request({'action': 'quotes', 'symbols': codes[pos:pos + 80]})
        rows = response.get('rows', []) if response else []
        for row in rows:
            code = ''.join(ch for ch in str(row.get('code') or '') if ch.isdigit())[-6:]
            if code:
                try:
                    out[code] = _quote_dict(row)
                except (TypeError, ValueError):
                    continue
    return out


def get_quote(symbol: str) -> Optional[dict]:
    code = ''.join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    return get_quotes([symbol]).get(code)


def _reset_for_tests() -> None:
    global _hard_unavailable
    with _lock:
        _drop_worker()
        _hard_unavailable = False


atexit.register(_drop_worker)
