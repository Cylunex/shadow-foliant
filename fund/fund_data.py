"""基金数据采集 —— 场外开放式基金为主,数据源 akshare(东财/雪球,免费、A股)。

设计:
  - 每个外部调用前 `throttle('akshare')` 自限流(复用 core/rate_limiter,防封)。
  - 取不到/网络异常一律返回 None 或空 DataFrame,绝不抛到上层(对齐项目其它数据模块)。
  - 全市场基金名单进程内缓存(列表大、变动慢),其它按需取。

主要能力(akshare 接口):
  list_funds            全市场名单+类型      fund_name_em
  get_fund_basic        基本信息(雪球)       fund_individual_basic_info_xq
  get_nav_history       历史净值(单位+累计)  fund_open_fund_info_em
  get_realtime_estimate 盘中净值估算         fund_value_estimation_em
  get_rank              同类排名             fund_open_fund_rank_em
  get_manager           基金经理             fund_manager_em
  get_stock_holdings    持股穿透             fund_portfolio_hold_em
  get_rating            评级                 fund_rating_all
"""

from __future__ import annotations

import threading
from typing import Optional

import pandas as pd

try:
    from rate_limiter import throttle
except Exception:  # 限流器缺失也不影响功能
    def throttle(key='akshare', min_interval=None):  # type: ignore
        return 0.0


def _ak():
    """惰性导入 akshare(未装则返回 None,调用方降级)。"""
    try:
        import akshare as ak
        return ak
    except Exception as e:
        print(f'[fund_data] akshare 未安装/不可用: {e}')
        return None


# ----- 全市场名单(进程内缓存) -----------------------------------------
_name_lock = threading.Lock()
_name_cache: Optional[pd.DataFrame] = None


def list_funds(force: bool = False) -> pd.DataFrame:
    """全市场场外基金名单。列:基金代码/拼音缩写/基金简称/基金类型/拼音全称。
    进程内缓存(force=True 强刷)。失败返回空 DataFrame。"""
    global _name_cache
    if _name_cache is not None and not force:
        return _name_cache
    with _name_lock:
        if _name_cache is not None and not force:
            return _name_cache
        ak = _ak()
        if ak is None:
            return pd.DataFrame()
        try:
            throttle('akshare')
            df = ak.fund_name_em()
            _name_cache = df
            return df
        except Exception as e:
            print(f'[fund_data] list_funds 失败: {e}')
            return pd.DataFrame()


def fund_name(code: str) -> Optional[str]:
    """按代码取基金简称(查名单缓存)。"""
    df = list_funds()
    if df is None or df.empty or '基金代码' not in df.columns:
        return None
    hit = df[df['基金代码'].astype(str) == str(code).zfill(6)]
    if len(hit):
        return str(hit.iloc[0].get('基金简称'))
    return None


def fund_type(code: str) -> Optional[str]:
    """按代码取基金类型(股票型/混合型/债券型/指数型/QDII/FOF/货币型...)。"""
    df = list_funds()
    if df is None or df.empty or '基金类型' not in df.columns:
        return None
    hit = df[df['基金代码'].astype(str) == str(code).zfill(6)]
    if len(hit):
        return str(hit.iloc[0].get('基金类型'))
    return None


# ----- 净值历史(核心) --------------------------------------------------
def get_nav_history(code: str, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
    """历史净值。返回标准 DataFrame[date, unit_nav, acc_nav, daily_return](升序)。
    单位净值走势 + 累计净值走势 合并。失败返回 None。
    start/end: 'YYYY-MM-DD' 可选区间过滤。"""
    ak = _ak()
    if ak is None:
        return None
    code = str(code).zfill(6)
    try:
        throttle('akshare')
        unit = ak.fund_open_fund_info_em(symbol=code, indicator='单位净值走势')
    except Exception as e:
        print(f'[fund_data] get_nav_history({code}) 单位净值失败: {e}')
        return None
    if unit is None or len(unit) == 0:
        return None
    unit = unit.rename(columns={'净值日期': 'date', '单位净值': 'unit_nav', '日增长率': 'daily_return'})
    df = unit[[c for c in ['date', 'unit_nav', 'daily_return'] if c in unit.columns]].copy()
    # 列结构护栏:akshare 接口字段若变更(如某些基金无标准列),'date'/'unit_nav' 可能缺失,
    # 后续 df['date']/to_datetime 会 KeyError 抛出 → 整个 nav 刷新任务当次中断。缺关键列直接降级 None。
    if 'date' not in df.columns or 'unit_nav' not in df.columns:
        print(f"[fund_data] get_nav_history({code}) 列结构异常,缺 date/unit_nav: {list(unit.columns)}")
        return None
    # 累计净值(可选,失败忽略)
    try:
        throttle('akshare')
        acc = ak.fund_open_fund_info_em(symbol=code, indicator='累计净值走势')
        if acc is not None and len(acc):
            acc = acc.rename(columns={'净值日期': 'date', '累计净值': 'acc_nav'})
            df = df.merge(acc[['date', 'acc_nav']], on='date', how='left')
    except Exception:
        pass
    if 'acc_nav' not in df.columns:
        df['acc_nav'] = df['unit_nav']
    df['date'] = pd.to_datetime(df['date'])
    for c in ('unit_nav', 'acc_nav', 'daily_return'):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)
    if start:
        df = df[df['date'] >= pd.to_datetime(start)]
    if end:
        df = df[df['date'] <= pd.to_datetime(end)]
    return df.reset_index(drop=True)


def latest_nav(code: str) -> Optional[dict]:
    """最新一条确认净值。返回 {date, unit_nav, acc_nav, daily_return} 或 None。
    akshare 净值历史失败时,回退 fundgz 的 dwjz(上一交易日确认净值)。"""
    df = get_nav_history(code)
    if df is None or df.empty:
        est = get_realtime_estimate(code)  # 兜底:fundgz dwjz
        if est and est.get('dwjz'):
            return {'date': est.get('jzrq'), 'unit_nav': est['dwjz'],
                    'acc_nav': est['dwjz'], 'daily_return': None}
        return None
    row = df.iloc[-1]
    return {
        'date': row['date'].strftime('%Y-%m-%d'),
        'unit_nav': float(row['unit_nav']) if pd.notna(row['unit_nav']) else None,
        'acc_nav': float(row['acc_nav']) if pd.notna(row.get('acc_nav')) else None,
        'daily_return': float(row['daily_return']) if pd.notna(row.get('daily_return')) else None,
    }


def get_peer_rank_percentile(code: str) -> Optional[dict]:
    """同类排名分位(雪球 fund_individual_achievement_xq 的「周期收益同类排名」rank/total)。
    取较长周期(近3年→近2年→近1年→今年以来→成立以来 优先)。
    返回 {period, rank, total, percentile(0-100,越大越靠前)} 或 None。"""
    ak = _ak()
    if ak is None:
        return None
    try:
        throttle('akshare')
        df = ak.fund_individual_achievement_xq(symbol=str(code).zfill(6))
        if df is None or df.empty:
            return None
        rank_col = next((c for c in df.columns if '同类排名' in c), None)
        period_col = next((c for c in df.columns if '周期' in c), None)
        if not rank_col or not period_col:
            return None
        pref = ['近3年', '近2年', '近1年', '今年以来', '成立以来']
        rows = {str(r[period_col]): str(r[rank_col]) for _, r in df.iterrows()}
        for p in pref:
            val = rows.get(p)
            if val and '/' in val:
                rk, tot = val.split('/')
                rk, tot = int(rk), int(tot)
                if tot > 0:
                    pct = (tot - rk + 1) / tot * 100
                    return {'period': p, 'rank': rk, 'total': tot, 'percentile': round(pct, 1)}
        return None
    except Exception as e:
        print(f'[fund_data] get_peer_rank_percentile({code}) 失败: {type(e).__name__}')
        return None


# ----- 基本信息 / 排名 / 经理 / 评级 / 持仓 -----------------------------
def get_fund_basic(code: str) -> Optional[dict]:
    """基金基本信息(雪球)。失败 None。"""
    ak = _ak()
    if ak is None:
        return None
    try:
        throttle('akshare')
        df = ak.fund_individual_basic_info_xq(symbol=str(code).zfill(6))
        if df is None or df.empty:
            return None
        # 雪球返回 item/value 两列
        if {'item', 'value'}.issubset(df.columns):
            return dict(zip(df['item'], df['value']))
        return df.iloc[0].to_dict()
    except Exception as e:
        print(f'[fund_data] get_fund_basic({code}) 失败: {e}')
        return None


def get_rank(fund_type_name: str = '全部') -> Optional[pd.DataFrame]:
    """同类排名榜。fund_type_name: 全部/股票型/混合型/债券型/指数型/QDII/LOF/FOF。"""
    ak = _ak()
    if ak is None:
        return None
    try:
        throttle('akshare')
        return ak.fund_open_fund_rank_em(symbol=fund_type_name)
    except Exception as e:
        print(f'[fund_data] get_rank({fund_type_name}) 失败: {e}')
        return None


# 天天基金盘中估值(JSONP,无需认证)。借鉴 leek-fund/funds/portfolio-tracker 都用此源。
_FUNDGZ_URL = 'https://fundgz.1234567.com.cn/js/{code}.js?rt={ts}'
_FUNDGZ_RE = None


def get_realtime_estimate(code: str) -> Optional[dict]:
    """盘中净值估算。**优先天天基金 fundgz 直连**(单基金、快),失败回退 akshare 全表。
    返回标准 dict:
      {code, name, dwjz(上一交易日单位净值), gsz(盘中估算净值), gszzl(估算涨跌%),
       gztime(估算时间), jzrq(净值日期), source}
    取不到返回 None。"""
    code = str(code).zfill(6)
    # —— Redis 缓存(盘中估值高频、跨进程共享;不可用自动降级)——
    try:
        from cache import cache_get, cache_set
    except Exception:
        cache_get = cache_set = None
    if cache_get:
        hit = cache_get(f'fund:rt:{code}')
        if hit:
            return hit
    # —— 主源:fundgz 直连 ——
    out = _fetch_fundgz(code)
    if out is None:
        out = _akshare_estimate(code)
    if out and cache_set:
        cache_set(f'fund:rt:{code}', out, ttl=30)
    return out


def _akshare_estimate(code: str) -> Optional[dict]:
    """akshare 全表估值兜底(fundgz 失败时)。"""
    code = str(code).zfill(6)
    # —— 兜底:akshare 全市场估值表 ——
    ak = _ak()
    if ak is None:
        return None
    try:
        throttle('akshare')
        df = ak.fund_value_estimation_em(symbol='全部')
        if df is None or df.empty:
            return None
        col_code = next((c for c in df.columns if '代码' in c), None)
        if not col_code:
            return None
        hit = df[df[col_code].astype(str) == code]
        if not len(hit):
            return None
        d = hit.iloc[0].to_dict()
        d['source'] = 'akshare'
        return d
    except Exception as e:
        print(f'[fund_data] get_realtime_estimate({code}) akshare 兜底失败: {e}')
        return None


def _fetch_fundgz(code: str) -> Optional[dict]:
    """直连天天基金 fundgz 盘中估值接口,JSONP 剥壳。失败返回 None(由上层降级)。"""
    global _FUNDGZ_RE
    import json
    import re
    import time
    import urllib.request
    if _FUNDGZ_RE is None:
        _FUNDGZ_RE = re.compile(r'jsonpgz\((.*)\)')
    url = _FUNDGZ_URL.format(code=code, ts=int(time.time() * 1000))
    try:
        throttle('eastmoney')  # 同属东财系,复用限流键
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'http://fund.eastmoney.com/'})
        raw = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', 'replace')
        m = _FUNDGZ_RE.search(raw)
        if not m:
            return None
        d = json.loads(m.group(1))
        return {
            'code': d.get('fundcode', code),
            'name': d.get('name'),
            'dwjz': _to_float(d.get('dwjz')),
            'gsz': _to_float(d.get('gsz')),
            'gszzl': _to_float(d.get('gszzl')),
            'gztime': d.get('gztime'),
            'jzrq': d.get('jzrq'),
            'source': 'fundgz',
        }
    except Exception as e:
        print(f'[fund_data] _fetch_fundgz({code}) 失败: {type(e).__name__}')
        return None


def _to_float(v):
    try:
        return float(v) if v not in (None, '', '--') else None
    except (ValueError, TypeError):
        return None


def get_stock_holdings(code: str, year: str = None) -> Optional[pd.DataFrame]:
    """持股穿透(前十大重仓股)。year 形如 '2024',缺省最近。失败 None。"""
    ak = _ak()
    if ak is None:
        return None
    try:
        throttle('akshare')
        kwargs = {'symbol': str(code).zfill(6)}
        if year:
            kwargs['date'] = year
        return ak.fund_portfolio_hold_em(**kwargs)
    except Exception as e:
        print(f'[fund_data] get_stock_holdings({code}) 失败: {e}')
        return None


def get_rating(code: str = None) -> Optional[pd.DataFrame]:
    """基金评级(全表或过滤单只)。失败 None。"""
    ak = _ak()
    if ak is None:
        return None
    try:
        throttle('akshare')
        df = ak.fund_rating_all()
        if df is None or df.empty or code is None:
            return df
        col_code = next((c for c in df.columns if '代码' in c), None)
        if col_code:
            return df[df[col_code].astype(str) == str(code).zfill(6)]
        return df
    except Exception as e:
        print(f'[fund_data] get_rating 失败: {e}')
        return None


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('名单条数:', len(list_funds()))
    print('000001 类型:', fund_type('000001'), '| 简称:', fund_name('000001'))
    nav = get_nav_history('000001')
    print('净值行数:', None if nav is None else len(nav))
    print('最新净值:', latest_nav('000001'))
