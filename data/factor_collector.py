import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
#!/usr/bin/env python3
"""
因子采集脚本 — 每日收盘后拉取 OHLCV + 估值因子，计算技术指标并存入快照表

用法:
  python3 factor_collector.py           # 仅采集并存储
  python3 factor_collector.py --score   # 采集 + 打分 + 排名
"""

import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(_bootstrap.ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import psycopg2
import time
from typing import Dict, List, Optional


def _native(val):
    """numpy/pandas → Python原生类型"""
    if val is None:
        return None
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, (np.integer,)):
        return int(val)
    import pandas as pd
    if pd.isna(val):
        return None
    return val

import os as _os
DB = dict(
    host=_os.getenv('PG_HOST', '127.0.0.1'), port=int(_os.getenv('PG_PORT', '55432')),
    user=_os.getenv('PG_USER', 'aiagents_stock'), password=_os.getenv('PG_PASSWORD', ''),
    dbname=_os.getenv('PG_DATABASE', 'aiagents_stock')
)


# ═══════════════════════════════════════════════════
#  OHLCV 采集
# ═══════════════════════════════════════════════════

def _yfinance_symbol(code: str) -> str:
    """转成 yfinance 格式：6开头→SS，其他→SZ"""
    if code.startswith('6'):
        return f'{code}.SS'
    return f'{code}.SZ'


def _days_to_period(days: int) -> str:
    for p, d in (('1mo', 30), ('3mo', 90), ('6mo', 180), ('1y', 365), ('2y', 730), ('3y', 1095)):
        if days <= d:
            return p
    return '3y'


def fetch_kline(symbol: str, days: int = 120) -> List[Dict]:
    """拉取单只股票日线 records [{trade_date,open,high,low,close,volume}]。
    ⭐ 因子库须用**前复权 qfq**(raw 在除权日假跳空 → 动量/52周高/均线类因子失真)。
    主源 datahub.kline(adjust='qfq')(标准 A股前复权,完整 OHLC,多源+磁盘缓存);
    兜底 Yahoo(走代理,用 adjclose 比例把 OHLC 也复权,口径近似 qfq、至少消除除权跳空)。"""
    # ── 主源:datahub 前复权(统一口径 + 复用缓存/多源)──
    try:
        import datahub
        df = datahub.kline(symbol, _days_to_period(days), adjust='qfq')
        if df is not None and not getattr(df, 'empty', True):
            recs = []
            for d, row in df.iterrows():
                c = row.get('Close')
                if c != c:   # NaN 跳过
                    continue
                recs.append({
                    'trade_date': str(d)[:10],
                    'open': _native(row.get('Open')), 'high': _native(row.get('High')),
                    'low': _native(row.get('Low')), 'close': _native(c),
                    'volume': int(_native(row.get('Volume')) or 0),
                })
            if recs:
                recs.sort(key=lambda r: r['trade_date'])
                return recs
    except Exception as e:
        print(f'  [kline] {symbol} datahub 取数失败, 转 Yahoo: {type(e).__name__}')

    # ── 兜底:Yahoo V8(走代理)。用 adjclose/close 比例把 OHLC 一并复权 → 近似前复权 ──
    import requests
    headers = {'User-Agent': 'Mozilla/5.0'}
    import config as _cfg
    proxies = _cfg.PROXIES.copy()
    if not proxies.get('http') and not proxies.get('https'):
        proxies = None
    suffix = '.SS' if symbol.startswith(('6', '9')) else '.SZ'
    yahoo_sym = f'{symbol}{suffix}'
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range={days}d&interval=1d'
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=15)
            if resp.status_code != 200:
                if attempt < 2:
                    time.sleep(2)
                    continue
                print(f'  [kline] {symbol} HTTP {resp.status_code}')
                return []
            data = resp.json()
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            ohlcv = result['indicators'].get('adjclose', [{}])
            adj = ohlcv[0].get('adjclose', [None] * len(timestamps)) if ohlcv else [None] * len(timestamps)
            records = []
            for i in range(len(timestamps)):
                if quote['close'][i] is not None:
                    d = datetime.fromtimestamp(timestamps[i]).strftime('%Y-%m-%d')
                    raw_c = _native(quote['close'][i])
                    # 复权因子 = adjclose/close;OHLC × 因子 → 复权 OHLC(消除除权跳空)
                    f = (float(adj[i]) / raw_c) if (adj[i] is not None and raw_c) else 1.0
                    records.append({
                        'trade_date': d,
                        'open': round(_native(quote['open'][i]) * f, 4),
                        'high': round(_native(quote['high'][i]) * f, 4),
                        'low': round(_native(quote['low'][i]) * f, 4),
                        'close': round(raw_c * f, 4),
                        'volume': int(_native(quote['volume'][i])),
                    })
            records.sort(key=lambda r: r['trade_date'])
            return records
        except Exception as e:
            if 'Too Many' in str(e) or 'Rate' in str(e):
                if attempt < 2:
                    time.sleep(3)
                    continue
            print(f'  [kline] {symbol} 失败: {e}')
            return []
    return []


def save_kline(cur, code: str, records: List[Dict]):
    for r in records:
        cur.execute("""
            INSERT INTO kline_daily (stock_code, trade_date, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stock_code, trade_date) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume
        """, (code, _native(r['trade_date']), _native(r['open']),
                _native(r['high']), _native(r['low']), _native(r['close']),
                _native(r['volume'])))


# ═══════════════════════════════════════════════════
#  技术指标计算
# ═══════════════════════════════════════════════════

def calc_ma(closes: np.ndarray, n: int) -> float:
    if len(closes) < n:
        return float(closes[-1])
    return float(np.mean(closes[-n:]))


def calc_rsi(closes: np.ndarray, n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    deltas = np.diff(closes[-n-1:])
    gains = np.sum(deltas[deltas > 0])
    losses = -np.sum(deltas[deltas < 0])
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def calc_macd(closes: np.ndarray):
    if len(closes) < 35:
        return None, None, None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if len(ema12) < 9:
        return None, None, None
    dif = ema12[-1] - ema26[-1]
    dea = _ema(np.array([dif] * 9 + [None]), 9)  # not great but works
    # better approach
    difs = np.array(ema12) - np.array(ema26)
    dea_line = _ema(difs, 9)
    if len(dea_line) == 0:
        return None, None, None
    hist = 2 * (difs[-1] - dea_line[-1])
    return difs[-1], dea_line[-1], hist


def _ema(data: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1)
    result = np.zeros_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def calc_max_dd(closes: np.ndarray, window: int = 60):
    if len(closes) < window:
        closes = closes
    else:
        closes = closes[-window:]
    cumulative_max = np.maximum.accumulate(closes)
    drawdowns = (closes - cumulative_max) / cumulative_max * 100
    return float(drawdowns.min())  # most negative value


def calc_interval_return(closes: np.ndarray, days: int) -> Optional[float]:
    if len(closes) <= days:
        return None
    return float((closes[-1] - closes[-days-1]) / closes[-days-1] * 100)


def calc_volatility(closes: np.ndarray, n: int = 20) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    daily_rets = np.diff(closes[-n-1:]) / closes[-n-1:-1]
    return float(np.std(daily_rets) * np.sqrt(252) * 100)


def compute_technical(closes: np.ndarray) -> Dict:
    result = {
        'ma_20': calc_ma(closes, 20),
        'ma_60': calc_ma(closes, 60) if len(closes) >= 60 else None,
        'ma_deviation_20': (closes[-1] - calc_ma(closes, 20)) / calc_ma(closes, 20) * 100,
        'rsi_14': calc_rsi(closes),
        'macd_dif': None,
        'macd_dea': None,
        'macd_hist': None,
        'ret_5d': calc_interval_return(closes, 5),
        'ret_20d': calc_interval_return(closes, 20),
        'ret_60d': calc_interval_return(closes, 60),
        'volatility_20': calc_volatility(closes),
        'max_dd_60': calc_max_dd(closes, 60),
    }
    # numpy → Python 原生类型（PG不接受np.float64）
    return {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
            for k, v in result.items()}


# ═══════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════

def collect(do_score: bool = False):
    from a_stock_data_adapter import adapter
    from portfolio_db_pg import portfolio_db

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    today = (datetime.now() + timedelta(hours=8)).strftime('%Y-%m-%d')

    stocks = portfolio_db.get_all_stocks()
    codes = [s.get('code', '') for s in stocks if s.get('code')]
    print(f'📊 采集 {len(codes)} 只股票因子数据 ({today})')

    # 批量行情
    quotes = adapter.get_quotes(codes)
    print(f'  行情获取: {len(quotes)}只')

    # 逐只处理
    snapshots = []
    kline_count = 0

    for i, code in enumerate(codes):
        q = quotes.get(code, {})
        if not q or not q.get('price'):
            continue

        # —— OHLCV采集 ——
        time.sleep(0.3)  # 限流保护
        klines = fetch_kline(code)
        if klines:
            save_kline(cur, code, klines)
            kline_count += len(klines)

        # —— 技术指标 ——
        if klines:
            closes = np.array([r['close'] for r in klines])
            tech = compute_technical(closes)
        else:
            tech = {}

        # —— 估值因子 ——
        val = {}
        try:
            val = adapter.get_full_valuation(code) or {}
        except Exception:
            pass

        snap = {
            'stock_code': code,
            'snapshot_date': today,
            'close': q.get('price'),
            'change_pct': q.get('change_pct'),
            'pe_ttm': q.get('pe_ttm'),
            'pb': q.get('pb'),
            'pe_fwd': val.get('pe_fwd'),
            'peg': val.get('peg'),
            'cagr_pct': val.get('cagr_pct'),
            'eps_cur': val.get('eps_cur'),
            'eps_next': val.get('eps_next'),
            'turnover_pct': q.get('turnover_pct'),
            'vol_ratio': q.get('vol_ratio'),
            'amplitude_pct': q.get('amplitude_pct'),
            'mcap_yi': q.get('mcap_yi'),
            **{k: tech.get(k) for k in ['ma_20', 'ma_60', 'ma_deviation_20',
                                         'rsi_14', 'macd_dif', 'macd_dea',
                                         'macd_hist', 'ret_5d', 'ret_20d',
                                         'ret_60d', 'volatility_20', 'max_dd_60']},
            'total_score': None,
            'rank_pos': None,
            'signal': None,
        }
        snapshots.append(snap)

        if (i + 1) % 10 == 0:
            print(f'  进度: {i+1}/{len(codes)}')

    # 写入快照
    for s in snapshots:
        fields = list(s.keys())
        placeholders = ', '.join(['%s'] * len(fields))
        cols = ', '.join(fields)
        cur.execute(f"""
            INSERT INTO factor_snapshots ({cols})
            VALUES ({placeholders})
            ON CONFLICT (stock_code, snapshot_date) DO UPDATE SET
                {', '.join(f'{f}=EXCLUDED.{f}' for f in fields if f not in ('stock_code','snapshot_date'))}
        """, [_native(s[k]) for k in fields])

    conn.commit()
    print(f'  ✅ 快照: {len(snapshots)}只  |  K线: {kline_count}条')

    # —— 打分 ——
    if do_score and snapshots:
        print('\n📊 综合打分（截面排名）...')
        from scripts.daily_signal_scan import _calc_stock_scores

        # 构建 portfolio_data
        portfolio_data = {}
        for s in stocks:
            code = s.get('code', '')
            portfolio_data[code] = {
                'name': s.get('name', code),
                'qty': s.get('quantity') or 0,
                'cost': s.get('cost_price') or 0,
                'return_pct': 0,
                'pos_value': (s.get('cost_price') or 0) * (s.get('quantity') or 0),
            }

        # 给 portfolio_data 补充当日收益率
        for code, pd_i in portfolio_data.items():
            q = quotes.get(code, {})
            price = q.get('price', 0) or 0
            cost = float(pd_i.get('cost', 0) or 0)
            qty_i = int(pd_i.get('qty', 0) or 0)
            if cost > 0:
                pd_i['return_pct'] = (price - cost) / cost * 100
                pd_i['pos_value'] = price * qty_i

        scores = _calc_stock_scores(quotes, portfolio_data)
        rank_map = {r['code']: r for r in scores}

        for s in snapshots:
            r = rank_map.get(s['stock_code'])
            if r:
                cur.execute("""
                    UPDATE factor_snapshots
                    SET total_score=%s, rank_pos=%s, signal=%s
                    WHERE stock_code=%s AND snapshot_date=%s
                """, (float(r['total_score']), int(r['rank']), str(r['signal']),
                      s['stock_code'], s['snapshot_date']))

        conn.commit()
        buy = len([r for r in scores if r['signal'] == 'BUY'])
        sell = len([r for r in scores if r['signal'] == 'SELL'])
        hold = len([r for r in scores if r['signal'] == 'HOLD'])
        print(f'  ✅ BUY:{buy} HOLD:{hold} SELL:{sell}')

    cur.close()
    conn.close()
    print('\n✅ 采集完成')


if __name__ == '__main__':
    collect(do_score='--score' in sys.argv)
