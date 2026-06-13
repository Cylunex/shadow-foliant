"""
数据源配置 — 策略筛选数据源
支持：auto / push2 / pywencai / dataapi
可通过环境变量 STRATEGY_DATA_SOURCE 切换

push2.eastmoney.com 有严格频率限制(实测≥8秒间隔才稳)
data.eastmoney.com/dataapi/xuangu/list 更稳定但字段有限
"""

import os
import time
import math
import random
import requests
import logging

logger = logging.getLogger(__name__)

# 当前选中的数据源策略
DATA_SOURCE = os.getenv('STRATEGY_DATA_SOURCE', 'auto').lower()


def get_strategy():
    return DATA_SOURCE


# push2 自限流 (实测过紧会被断连)
_PUSH2_LAST_CALL = 0
_PUSH2_MIN_INTERVAL = 3.0  # 秒 — 东财限流间隔(太松耗时太长)
_PUSH2_CALL_COUNT = 0       # 当前批次第几次调用


def _push2_throttle():
    """push2 自限流"""
    global _PUSH2_LAST_CALL, _PUSH2_CALL_COUNT
    _PUSH2_CALL_COUNT += 1
    elapsed = time.time() - _PUSH2_LAST_CALL
    if elapsed < _PUSH2_MIN_INTERVAL:
        sleep_time = _PUSH2_MIN_INTERVAL - elapsed
        logger.info('[push2] 限流等待 %.1fs (第%s次)' % (sleep_time, _PUSH2_CALL_COUNT))
        time.sleep(sleep_time)
    _PUSH2_LAST_CALL = time.time()


# rate_limiter (pywencai用)
try:
    from rate_limiter import throttle as _throttle
except Exception:
    def _throttle(*a, **k):
        return 0.0


# 东财push2字段: f2=现价 f3=涨跌% f7=净利润增长% f9=PE(动)
# f12=代码 f14=名称 f20=总市值(亿) f23=PB f25=最新价
FIELDS = 'f2,f3,f7,f9,f12,f14,f20,f23,f25,f37'

# 全A股(沪深主板+创业板+科创板) — 不含北交所，不含ST
MARKET_FS = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
# 不含科创板
MARKET_FS_NO_KCB = 'm:0+t:6,m:0+t:80,m:1+t:2'


def fetch_stocks_push2(
    price_max=None, pe_max=None, profit_growth_min=None,
    mcap_max=None, mcap_min=None,
    top_n=10, sort_field='f3', sort_asc=False,
    include_kcb=False,
):
    """
    用东财push2 clist API 筛选股票

    注意: ft过滤参数不稳定,改为取全量+客户端过滤
    """
    _push2_throttle()

    # 取前200只按排序字段排列,客户端再做条件过滤
    fetch_n = max(top_n * 5, 200)
    params = {
        'pn': 1, 'pz': fetch_n, 'po': 1 if sort_asc else 0,
        'np': 1,
        'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
        'fltt': 2, 'invt': 2, 'fid': sort_field,
        'fs': MARKET_FS if include_kcb else MARKET_FS_NO_KCB,
        'fields': FIELDS,
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://quote.eastmoney.com/',
    }

    retries = 3
    for attempt in range(retries):
        try:
            r = requests.get(
                'https://push2.eastmoney.com/api/qt/clist/get',
                params=params, headers=headers, timeout=15,
            )
            if r.status_code != 200:
                if attempt < retries - 1:
                    time.sleep(5 + attempt * 3)
                    continue
                return {'success': False, 'data': [], 'msg': 'HTTP %s' % r.status_code}
            d = r.json()
            total = d.get('data', {}).get('total', 0)
            items = d.get('data', {}).get('diff', [])
            result = []
            for item in items:
                code = str(item.get('f12', ''))
                # 客户端兜底过滤科创板
                if not include_kcb and code.startswith('688'):
                    continue
                name = str(item.get('f14', ''))
                price = item.get('f2')
                pe = item.get('f9')
                growth = item.get('f7')
                mcap = item.get('f20')

                try:
                    price_f = float(price) if price is not None else None
                    pe_f = float(pe) if pe is not None else None
                    growth_f = float(growth) if growth is not None else None
                    mcap_f = float(mcap) if mcap is not None else None
                except (ValueError, TypeError):
                    continue
                if price_max is not None and (price_f is None or price_f > price_max):
                    continue
                if pe_max is not None and (pe_f is None or pe_f > pe_max):
                    continue
                if profit_growth_min is not None and (growth_f is None or growth_f < profit_growth_min):
                    continue
                if mcap_max is not None and (mcap_f is None or mcap_f / 1e8 > mcap_max):
                    continue
                if mcap_min is not None and (mcap_f is None or mcap_f / 1e8 < mcap_min):
                    continue

                result.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'pe': pe,
                    'growth': growth,       # f7 净利润增长率
                    'change_pct': change_pct,  # f3 涨跌幅
                    'mcap': mcap,
                    'pb': item.get('f23'),
                })

            # 取前top_n
            result = result[:top_n]
            return {'success': True, 'data': result, 'msg': '共%s只(过滤后%s只)' % (total, len(result))}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5 + attempt * 3)
                continue
            return {'success': False, 'data': [], 'msg': str(e)}
    return {'success': False, 'data': [], 'msg': 'max retries'}


def screen_stocks(
    price_max=None, pe_max=None, profit_growth_min=None,
    mcap_max=None, mcap_min=None,
    top_n=10, sort_field='f3', sort_asc=False,
    include_kcb=False,
):
    """
    统一选股入口，自动切换数据源
    环境变量 STRATEGY_DATA_SOURCE: auto / push2 / pywencai / dataapi
    """
    strategy = get_strategy()

    # dataapi 直接走东财选股器，跳过 push2 / pywencai
    if strategy == 'dataapi':
        try:
            result = fetch_stocks_dataapi(
                price_max=price_max, pe_max=pe_max,
                profit_growth_min=profit_growth_min,
                mcap_max=mcap_max, mcap_min=mcap_min,
                top_n=top_n, sort_field=sort_field, sort_asc=sort_asc,
                include_kcb=include_kcb,
            )
            if result['success']:
                return result
            logger.warning('dataapi失败: %s' % result['msg'])
        except Exception as e:
            logger.warning('dataapi异常: %s' % e)
        return {'success': False, 'data': [], 'msg': 'dataapi不可用'}

    if strategy in ('auto', 'push2'):
        result = fetch_stocks_push2(
            price_max=price_max, pe_max=pe_max,
            profit_growth_min=profit_growth_min,
            mcap_max=mcap_max, mcap_min=mcap_min,
            top_n=top_n, sort_field=sort_field, sort_asc=sort_asc,
            include_kcb=include_kcb,
        )
        if result['success']:
            return result
        logger.warning('push2失败: %s, 尝试pywencai' % result['msg'])
        if strategy == 'push2':
            # 不直接 return, 继续尝试 fallback
            pass

    # pywencai fallback
    try:
        import pywencai
        import pandas as pd
        parts = []
        if price_max is not None:
            parts.append('股价<%s元' % price_max)
        if pe_max is not None:
            parts.append('市盈率<%s' % pe_max)
        if profit_growth_min is not None:
            parts.append('净利润增长率>=%s%%' % profit_growth_min)
        if mcap_max is not None:
            parts.append('总市值<%s亿' % mcap_max)
        parts.extend(['非st', '非科创板', '非创业板', '沪深A股'])
        query = '，'.join(parts) + '，成交额由小至大排名'

        _throttle('pywencai')
        import threading
        result_container = []
        def _pywencai_call():
            try:
                result_container.append(pywencai.get(query=query, loop=True))
            except Exception:
                result_container.append(None)
        t = threading.Thread(target=_pywencai_call, daemon=True)
        t.start()
        t.join(timeout=30)
        if t.is_alive():
            logger.warning('pywencai 超时(30s)，降级到 dataapi')
            result = None
        else:
            result = result_container[0] if result_container else None
        if result is not None:
            if isinstance(result, pd.DataFrame):
                return {'success': True, 'data': result.to_dict('records'), 'msg': 'pywencai: %s只' % len(result)}
            return {'success': True, 'data': result, 'msg': 'pywencai成功'}
    except Exception as e:
        logger.warning('pywencai失败: %s' % e)

    # dataapi fallback — 走 data.eastmoney.com (push2/pywencai 都不可用时的兜底)
    try:
        result = fetch_stocks_dataapi(
            price_max=price_max, pe_max=pe_max,
            profit_growth_min=profit_growth_min,
            mcap_max=mcap_max, mcap_min=mcap_min,
            top_n=top_n, sort_field=sort_field, sort_asc=sort_asc,
            include_kcb=include_kcb,
        )
        if result['success']:
            return result
        logger.warning('dataapi失败: %s' % result['msg'])
    except Exception as e:
        logger.warning('dataapi异常: %s' % e)

def fetch_stocks_dataapi(
    price_max=None, pe_max=None, profit_growth_min=None,
    mcap_max=None, mcap_min=None,
    top_n=10, sort_field='f37', sort_asc=True,
    include_kcb=False,
):
    """
    用东财选股器 data.eastmoney.com/dataapi/xuangu/list 筛选股票
    push2 不可用时的兜底方案。

    注意：盘前 NEW_PRICE 可能为 '-'(字符串) 而非数值，
    此时不做服务端价格过滤，改为客户端过滤。
    """
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    session = requests.Session()
    session.headers.update({
        'User-Agent': ua,
        'Referer': 'https://data.eastmoney.com/xuangu/',
    })

    # 东财选股器字段 — 只请求必要字段提升效率
    col_sty_map = {
        'code': 'SECURITY_CODE',
        'name': 'SECURITY_NAME_ABBR',
        'price': 'NEW_PRICE',
        'pe': 'PE_TTM',
        'mcap': 'TOTAL_MARKET_CAP',
        'pb': 'PB',
    }
    sty = ','.join(col_sty_map.values())

    # 服务端过滤 — 仅过滤 PE / 市值（这些字段始终是数值）
    # 价格过滤放客户端，避免盘前 '-' 导致 ANTLR 错误或空结果
    server_filters = []
    if pe_max is not None:
        server_filters.append('(PE_TTM<=%s)' % pe_max)
    if mcap_max is not None:
        server_filters.append('(TOTAL_MARKET_CAP<=%s)' % (mcap_max * 1e8))
    if mcap_min is not None:
        server_filters.append('(TOTAL_MARKET_CAP>=%s)' % (mcap_min * 1e8))

    filter_str = ''.join(server_filters)

    url = 'https://data.eastmoney.com/dataapi/xuangu/list'
    params = {
        'sty': sty,
        'filter': filter_str,
        'p': 1,
        'ps': max(top_n * 5, 200),  # 多取一些供客户端过滤
        'source': 'SELECT_SECURITIES',
        'client': 'WEB',
    }

    is_numeric = lambda v: v is not None and v not in ('-', '', '--', 'None', 'null')
    to_float = lambda v: float(v) if is_numeric(v) else None

    retries = 2
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code != 200:
                if attempt < retries - 1:
                    time.sleep(random.uniform(1, 3))
                    continue
                return {'success': False, 'data': [], 'msg': 'HTTP %s' % r.status_code}
            j = r.json()
            if not j.get('success') or not j.get('result', {}).get('data'):
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return {'success': False, 'data': [], 'msg': 'api返回空: %s' % j.get('message', 'unknown')}

            rows = j['result']['data']
            total = j['result'].get('count', len(rows))
            result = []
            for item in rows:
                code = str(item.get('SECURITY_CODE', ''))
                # 客户端兜底过滤科创板
                if not include_kcb and code.startswith('688'):
                    continue
                name = str(item.get('SECURITY_NAME_ABBR', ''))
                raw_price = item.get('NEW_PRICE')
                raw_pe = item.get('PE_TTM')
                raw_mcap = item.get('TOTAL_MARKET_CAP')
                raw_pb = item.get('PB')

                price_f = to_float(raw_price)
                pe_f = to_float(raw_pe)
                mcap_f = to_float(raw_mcap)
                mcap_f_亿 = mcap_f / 1e8 if mcap_f is not None else None

                # 客户端价格过滤
                if price_max is not None and (price_f is None or price_f > price_max):
                    continue
                if mcap_max is not None and (mcap_f_亿 is None or mcap_f_亿 > mcap_max):
                    continue
                if mcap_min is not None and (mcap_f_亿 is None or mcap_f_亿 < mcap_min):
                    continue

                result.append({
                    'code': code,
                    'name': name,
                    'price': raw_price,
                    'pe': raw_pe,
                    'growth': None,  # dataapi 不直接提供净利润增长率
                    'mcap': raw_mcap,
                    'pb': raw_pb,
                })

            result = result[:top_n]
            return {'success': True, 'data': result, 'msg': 'dataapi: 共%s只(过滤后%s只)' % (total, len(result))}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(random.uniform(1, 3))
                continue
            return {'success': False, 'data': [], 'msg': str(e)}
    return {'success': False, 'data': [], 'msg': 'max retries'}
