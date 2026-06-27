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


def _fund_list_eastmoney() -> Optional[pd.DataFrame]:
    """直连天天基金 fundcode_search.js 取全市场名单(实测 ~0.4s,远快于 akshare fund_name_em ~7s)。
    返回与 akshare fund_name_em 同列名的 DataFrame(基金代码/拼音缩写/基金简称/基金类型/拼音全称),失败 None。"""
    import json as _json
    import re as _re
    import urllib.request
    try:
        throttle('eastmoney')
        req = urllib.request.Request('https://fund.eastmoney.com/js/fundcode_search.js',
                                     headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'http://fund.eastmoney.com/'})
        txt = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', 'replace')
        m = _re.search(r'var\s+r\s*=\s*(\[.*\]);', txt, _re.S)
        if not m:
            return None
        arr = _json.loads(m.group(1))   # [[code, pinyin_abbr, name, type, pinyin_full], ...]
        if not arr:
            return None
        return pd.DataFrame(arr, columns=['基金代码', '拼音缩写', '基金简称', '基金类型', '拼音全称'])
    except Exception as e:
        print(f'[fund_data] _fund_list_eastmoney 失败: {type(e).__name__}')
        return None


def list_funds(force: bool = False) -> pd.DataFrame:
    """全市场场外基金名单。列:基金代码/拼音缩写/基金简称/基金类型/拼音全称。
    **优先直连东财 fundcode_search(快~0.4s),失败回退 akshare fund_name_em(~7s)。**
    进程内缓存(force=True 强刷)。失败返回空 DataFrame。"""
    global _name_cache
    if _name_cache is not None and not force:
        return _name_cache
    with _name_lock:
        if _name_cache is not None and not force:
            return _name_cache
        df = _fund_list_eastmoney()   # 主源:直连东财(快)
        if df is None or df.empty:
            ak = _ak()                # 兜底:akshare
            if ak is None:
                return pd.DataFrame()
            try:
                throttle('akshare')
                df = ak.fund_name_em()
            except Exception as e:
                print(f'[fund_data] list_funds 失败: {e}')
                return pd.DataFrame()
        _name_cache = df
        return df


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


# ----- 净值历史 --------------------------------------------------------
# ⭐ 历史净值统一走 datahub(主源东财 lsjz JSON + 兜底 akshare, 健康度路由 + 缓存)。
# 这里保留 get_nav_history 函数签名是为兼容上层调用方; 真正取数在 data.datahub.fund_nav_history。
def get_nav_history(code: str, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
    """历史净值(瘦封装,实际走 datahub.fund_nav_history)。
    返回标准 DataFrame[date, unit_nav, acc_nav, daily_return](升序),失败 None。
    start/end: 'YYYY-MM-DD' 可选区间过滤。"""
    from data.datahub import fund_nav_history
    return fund_nav_history(str(code).zfill(6), start=start, end=end)


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


# ----- 基金重仓股(季报数据,文件缓存防逐只重复打东财) -------------------
# fund_portfolio_hold_em 走东财天天基金、是**季报**披露(一季度才变一次)。基金组合诊断的重仓穿透
# 会对持有的 N 只基金逐只调本接口,无缓存时盘中一点就逐只串行打几十次东财 → 易被封 IP。
# 故:① 结果按 基金代码+年份 落 1 天文件缓存(DataFrame,L2 pickle 思路,无 Redis 也在);
#     ② cache_only=True(组合穿透盘中传)→ 只读缓存、冷则返回过期缓存或 None(绝不盘中现拉);
#     ③ 现取失败回退过期缓存(季报数据旧一天无妨,有胜无)。
import os as _os
import time as _time
try:
    import _bootstrap as _bs
    _HOLD_CACHE_DIR = _os.path.join(_bs.DB_DIR, 'fund_holdings_cache')
except Exception:
    _HOLD_CACHE_DIR = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'db', 'fund_holdings_cache')
_HOLD_TTL = 24 * 3600   # 1 天:季报数据日内不变,盘后焐一次日内复用;次日自然刷新


def _read_hold_pkl(cf: str) -> Optional[pd.DataFrame]:
    try:
        if _os.path.isfile(cf):
            df = pd.read_pickle(cf)
            if isinstance(df, pd.DataFrame):
                return df
    except Exception:
        pass
    return None


def get_stock_holdings(code: str, year: str = None,
                       cache_only: bool = False) -> Optional[pd.DataFrame]:
    """持股穿透(前十大重仓股,季报)。year 形如 '2024',缺省最近。失败 None。
    cache_only=True:只读缓存、缓存冷不现拉东财(返回过期缓存或 None)——组合重仓穿透盘中用,防逐只封 IP。"""
    code6 = str(code).zfill(6)
    cf = _os.path.join(_HOLD_CACHE_DIR, f"{code6}_{year or 'latest'}.pkl")
    # ① 新鲜缓存命中(TTL 内)
    try:
        if _os.path.isfile(cf) and (_time.time() - _os.path.getmtime(cf)) < _HOLD_TTL:
            fresh = _read_hold_pkl(cf)
            if fresh is not None:
                return fresh
    except Exception:
        pass
    # ② 盘中只读模式:缓存冷不现拉(有过期缓存就用旧的,季报旧一天无妨;全无 → None 让上层跳过)
    if cache_only:
        return _read_hold_pkl(cf)
    # ③ 盘后/显式现取 → 写缓存
    ak = _ak()
    if ak is None:
        return _read_hold_pkl(cf)
    try:
        throttle('akshare')
        kwargs = {'symbol': code6}
        if year:
            kwargs['date'] = year
        df = ak.fund_portfolio_hold_em(**kwargs)
        if isinstance(df, pd.DataFrame) and not df.empty:
            try:
                _os.makedirs(_HOLD_CACHE_DIR, exist_ok=True)
                df.to_pickle(cf)
            except Exception:
                pass
        return df
    except Exception as e:
        print(f'[fund_data] get_stock_holdings({code}) 失败: {e}')
        return _read_hold_pkl(cf)   # 现取失败回退过期缓存,有胜无


def top_holdings(code: str, n: int = 10) -> dict:
    """前 N 大重仓股(最新季度)。返回 {quarter, holdings:[{code,name,pct,mv}]}。失败返回空。"""
    df = get_stock_holdings(code)
    if df is None or len(df) == 0:
        return {'quarter': None, 'holdings': []}
    q_col = next((c for c in df.columns if '季度' in c), None)
    name_col = next((c for c in df.columns if '股票名称' in c), None)
    code_col = next((c for c in df.columns if '股票代码' in c), None)
    pct_col = next((c for c in df.columns if '占净值比例' in c), None)
    mv_col = next((c for c in df.columns if '持仓市值' in c), None)
    latest = None
    if q_col:
        try:
            latest = sorted(df[q_col].dropna().astype(str).unique())[-1]
            df = df[df[q_col].astype(str) == latest]
        except Exception:
            pass
    if pct_col:
        df = df.sort_values(pct_col, ascending=False)
    out = []
    for _, r in df.head(n).iterrows():
        out.append({
            'code': str(r.get(code_col)) if code_col else None,
            'name': str(r.get(name_col)) if name_col else None,
            'pct': _to_float(r.get(pct_col)) if pct_col else None,
            'mv': _to_float(r.get(mv_col)) if mv_col else None,
        })
    return {'quarter': latest, 'holdings': out}


def rating_summary(code: str) -> dict:
    """评级摘要:基金经理/公司/各机构星级/手续费。失败返回 {}。"""
    df = get_rating(code)
    if df is None or len(df) == 0:
        return {}
    row = df.iloc[0]
    keep = ['基金经理', '基金公司', '5星评级家数', '上海证券', '招商证券', '济安金信', '晨星评级', '手续费']
    out = {}
    for k in keep:
        if k not in df.columns:
            continue
        v = row.get(k)
        if pd.isna(v):
            out[k] = None
        elif isinstance(v, (int, float)) or hasattr(v, 'item'):
            try:
                out[k] = float(v)
            except Exception:
                out[k] = str(v)
        else:
            out[k] = str(v)
    return out


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
