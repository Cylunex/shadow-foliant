# -*- coding: utf-8 -*-
"""统一外部数据层(datahub)—— 全项目取外部数据的唯一门面。

设计(2026-06-12):
  上层(选股/分析/jobs/webui/AI工作流)只 import 本模块,永远拿到**同一套标准格式**;
  本层负责:① 按"数据域"组织接口 ② **自适应源路由**(_route:按成功率/延迟/冷却自动升降级源)
            ③ 把各源异构返回规整成标准格式。
  底下的真实取数源(保留各自实现,本层只编排,不重写):
    - a_stock_data_adapter   腾讯/东财(非push2)/新浪/同花顺/百度  —— A股主力 HTTP 直连
    - stock_data.StockDataFetcher  K线+技术指标(内部已多源:东财/akshare/Ashare/mootdx)
    - akshare / tushare / pywencai  兜底源(按需,装了才用)
    - data_source_manager / 北向缓存 等专用编排(datahub 调它们,它们不回调 datahub,无循环)

自适应升降级(_route):
  每个数据域有一条"具名源链"。每次取数按各源**健康度**(成功率为主、延迟轻罚、连续失败进冷却重罚)
  动态排序后依次试,返回第一个非空结果,并记录本次成功/失败/延迟。于是:稳定快的源自动上位、
  老挂的源自动靠后(冷却期内基本不试),无需改代码。统计 `source_stats()` 可观测。
  注:统计是进程内的(重启后快速重新学习);跨进程不共享,够用。

标准返回格式(契约,上层只认这个):
  · 行情 quote/quotes : dict,键 = code/name/price/last_close/open/high/low/
      change_pct(%)/change_amt/amount/turnover_pct/pe_ttm/pb/mcap_yi/vol_ratio
  · K线 kline         : pandas.DataFrame,列 = date/open/high/low/close/volume/p_change(可含指标列)
  · 个股信息 stock_info: dict;指数 indices: list[{name,value,change_amt,change_pct}]
  · 列表类(北向/龙虎榜/资金流/板块/新闻/财报): list[dict],键名见各函数 docstring
  取数失败统一返回"空值"(dict→{}, list→[], DataFrame→空 DF),绝不抛异常打断上层。

加新数据源:在对应域 _route(...) 的源链里加一个 (名, thunk) 即可,会自动纳入升降级竞争。

统一缓存层(2026-06-13,避免频繁外部调用):
  非实时数据域统一套三级缓存:L0 进程内存(最快)→ L1 Redis(跨进程,复用 core.cache,仅
  JSON 友好值)→ L2 本地文件 pickle(durable、含 DataFrame、重启/无 Redis 也在)。miss 才走
  _route 真取并回填各级。每"数据域"按数据时效性配 TTL(见 _CACHE_TTL);(盘中, 盘外) 二元组的
  域盘中用短 TTL、盘外拉长。实时域(报价/指数/分钟资金流)盘中秒级即足以削掉重复突发请求又不失鲜。
  · 任意取数函数加 `use_cache=False` 可绕过缓存只取本次实时;全局 env DATAHUB_CACHE=false 全关。
  · K线另有专用磁盘缓存(见 kline()),不在此层重复包。
  · 优雅降级:Redis 挂→只用内存+文件;文件不可写→只用内存;全挂→等价直算。
  · 观测/清理:datahub.cache_stats() / datahub.cache_clear(domain=None)。
  · 返回值视为只读(与项目惯例一致),勿对其原地修改(内存级命中是同一对象引用)。
"""
from __future__ import annotations

import time as _time
from typing import Any, Callable, Dict, List, Tuple

import _bootstrap  # noqa: F401

import pandas as pd


# ══════════════════════════════════════════════════════════
#  自适应源路由引擎(按使用情况自动升降级)
# ══════════════════════════════════════════════════════════

_COOLDOWN_SEC = 120        # 连续失败的源降级冷却时长
_COOLDOWN_FAILS = 2        # 连续失败达到此数 → 进入冷却(排到最后)


class _Stat:
    __slots__ = ('ok', 'fail', 'lat_sum', 'lat_n', 'last_ok', 'last_fail', 'streak_fail')

    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.lat_sum = 0.0
        self.lat_n = 0
        self.last_ok = 0.0
        self.last_fail = 0.0
        self.streak_fail = 0


_STATS: Dict[str, _Stat] = {}


def _health(key: str, now: float) -> float:
    """源健康度评分(越高越优先)。未知源给中性偏高分(鼓励探索);
    成功率为主导,平均延迟轻微扣分;连续失败且在冷却期 → 重罚沉底。"""
    s = _STATS.get(key)
    if s is None:
        return 1.0
    total = s.ok + s.fail
    if total == 0:
        return 1.0
    rate = s.ok / total
    avg_lat = (s.lat_sum / s.lat_n) if s.lat_n else 1.0
    score = rate - min(avg_lat, 10.0) / 50.0   # 延迟最多扣 0.2
    if s.streak_fail >= _COOLDOWN_FAILS and (now - s.last_fail) < _COOLDOWN_SEC:
        score -= 1.0
    return score


def _record(key: str, ok: bool, latency: float):
    s = _STATS.get(key)
    if s is None:
        s = _STATS[key] = _Stat()
    now = _time.time()
    s.lat_sum += latency
    s.lat_n += 1
    if ok:
        s.ok += 1
        s.last_ok = now
        s.streak_fail = 0
    else:
        s.fail += 1
        s.last_fail = now
        s.streak_fail += 1


def _route(capability: str, sources: List[Tuple[str, Callable[[], Any]]], empty=None):
    """按健康度动态排序源链,依次试,返回第一个"非空"结果并记录统计(供自动升降级)。
    sources: [(源名, 无参thunk), ...]。DataFrame 用 not empty 判空,其余用 truthy。单源异常被吞续试下一个。"""
    now = _time.time()
    ordered = sorted(sources, key=lambda ns: -_health(f"{capability}:{ns[0]}", now))
    for name, fn in ordered:
        key = f"{capability}:{name}"
        t0 = _time.time()
        try:
            v = fn()
        except Exception:
            _record(key, False, _time.time() - t0)
            continue
        good = (not v.empty) if isinstance(v, pd.DataFrame) else bool(v)
        _record(key, good, _time.time() - t0)
        if good:
            return v
    return empty


def source_stats() -> Dict[str, dict]:
    """各源运行统计(观测/调试用)。键 = '域:源名'。"""
    now = _time.time()
    out = {}
    for key, s in _STATS.items():
        total = s.ok + s.fail
        out[key] = {
            'ok': s.ok, 'fail': s.fail,
            'rate': round(s.ok / total, 3) if total else None,
            'avg_ms': round(s.lat_sum / s.lat_n * 1000) if s.lat_n else None,
            'streak_fail': s.streak_fail,
            'cooling': s.streak_fail >= _COOLDOWN_FAILS and (now - s.last_fail) < _COOLDOWN_SEC,
        }
    return out


# ══════════════════════════════════════════════════════════
#  统一缓存层(L0 内存 → L1 Redis → L2 文件 pickle;非实时域避免频繁外部调用)
# ══════════════════════════════════════════════════════════
import os as _os
import functools as _ft
import hashlib as _hashlib
import pickle as _pickle
import threading as _threading

_CACHE_ON = _os.getenv("DATAHUB_CACHE", "true").lower() not in ("false", "0", "no")
_CACHE_DIR = _os.path.join(_bootstrap.DB_DIR, "datahub_cache")
_MEM: Dict[str, Tuple[float, Any]] = {}
_MEM_LOCK = _threading.Lock()
_MEM_MAX = 4000
_CACHE_HITS: Dict[str, int] = {}
_CACHE_MISS: Dict[str, int] = {}

# 每"数据域"缓存 TTL(秒)。int=固定;(盘中, 盘外) 二元组=交易时段/非交易时段不同。
# 未列出的域用 _DEFAULT_TTL。设计依据=该域数据的真实更新频率(报价秒级、财报按天)。
_CACHE_TTL: Dict[str, Any] = {
    # —— 实时域:盘中秒级(削重复突发请求,不失鲜)、盘外拉长 ——
    "quotes": (10, 1800),
    "indices": (20, 1800),
    "capital_flow_minute": (30, 1800),
    # —— 盘中缓慢变化:板块/热点/题材 ——
    "hot_stocks": (1800, 21600),
    "sector_ranking": (1800, 21600),
    "sector_spot": (1800, 21600),
    "sector_fund_flow": (1800, 21600),
    # —— 日级(收盘后才更新):资金流/龙虎榜/北向 ——
    "north_flow": 10800,
    "capital_flow": 21600,
    "dragon_tiger": 21600,
    "dragon_tiger_stock": 21600,
    "kline_with_indicators": (3600, 43200),
    # —— 低频(按天/更久):基本面/估值/股东/解禁/概念 ——
    "stock_info": 43200,
    "financials": 86400,
    "valuation": (3600, 43200),
    "full_valuation": (3600, 43200),
    "eps_forecast": 86400,
    "concept_blocks": 604800,
    "margin": 86400,
    "block_trade": 86400,
    "holder_num_change": 86400,
    "dividend_history": 86400,
    "lockup_expiry": 86400,
    # —— 新闻/公告/选股 ——
    "stock_news": 1800,
    "market_news": 900,
    "announcements": 3600,
    "screen": 1800,
    "convertible_bonds": (1800, 21600),
    # —— 基金:历史净值每天 17:00~21:00 公布一次, 收盘前不变 ——
    "fund_nav": (3600, 7200),
}
_DEFAULT_TTL = 3600


def _is_trading_hours() -> bool:
    """工作日 09:15–15:05 视为盘中(用于挑选实时域的短 TTL)。异常按非盘中。"""
    try:
        from datetime import datetime as _dt
        n = _dt.now()
        if n.weekday() >= 5:
            return False
        m = n.hour * 60 + n.minute
        return (9 * 60 + 15) <= m <= (15 * 60 + 5)
    except Exception:
        return False


def _ttl_for(domain: str) -> int:
    spec = _CACHE_TTL.get(domain, _DEFAULT_TTL)
    if isinstance(spec, tuple):
        return spec[0] if _is_trading_hours() else spec[1]
    return int(spec)


def _mk_key(domain: str, args, kwargs) -> str:
    sig = repr(args) + "|" + repr(sorted(kwargs.items()))
    h = _hashlib.md5(sig.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{domain}:{h}"


def _nonempty(v) -> bool:
    """是否值得缓存(空结果不缓存 → 下次自动重试真取)。"""
    if v is None:
        return False
    if isinstance(v, pd.DataFrame):
        return not v.empty
    if isinstance(v, dict) and "success" in v:   # screen() 形态:失败不缓存
        return bool(v.get("success"))
    return bool(v)


def _cache_file(key: str) -> str:
    domain, name = key.split(":", 1)
    return _os.path.join(_CACHE_DIR, domain, name + ".pkl")


def _cache_get(key: str, ttl: int):
    now = _time.time()
    # L0 内存
    with _MEM_LOCK:
        ent = _MEM.get(key)
    if ent and (now - ent[0]) < ttl:
        v = ent[1]
        return v.copy() if isinstance(v, pd.DataFrame) else v
    # L1 Redis(JSON 友好值;DataFrame 不走 Redis)
    try:
        from core import cache as _rc
        v = _rc.cache_get("dh:" + key)
        if v is not None:
            with _MEM_LOCK:
                _MEM[key] = (now, v)
            return v
    except Exception:
        pass
    # L2 本地文件(pickle,含 DataFrame;按 mtime 判 TTL)
    try:
        f = _cache_file(key)
        if _os.path.isfile(f) and (now - _os.path.getmtime(f)) < ttl:
            with open(f, "rb") as fh:
                v = _pickle.load(fh)
            with _MEM_LOCK:
                _MEM[key] = (now, v)
            return v
    except Exception:
        pass
    return None


def _cache_put(key: str, val, ttl: int):
    now = _time.time()
    with _MEM_LOCK:
        if len(_MEM) >= _MEM_MAX:   # 软上限:淘汰最旧 1/4
            for k in sorted(_MEM, key=lambda k: _MEM[k][0])[: _MEM_MAX // 4]:
                _MEM.pop(k, None)
        _MEM[key] = (now, val)
    if not isinstance(val, pd.DataFrame):   # DataFrame 只落文件
        try:
            from core import cache as _rc
            _rc.cache_set("dh:" + key, val, ttl)
        except Exception:
            pass
    try:
        f = _cache_file(key)
        _os.makedirs(_os.path.dirname(f), exist_ok=True)
        with open(f, "wb") as fh:
            _pickle.dump(val, fh)
    except Exception:
        pass


def _dh_cache(domain: str):
    """给非实时取数函数套统一缓存的装饰器。use_cache=False 或 DATAHUB_CACHE=false 绕过。"""
    def deco(fn):
        @_ft.wraps(fn)
        def wrap(*args, **kwargs):
            if not _CACHE_ON or kwargs.pop("use_cache", True) is False:
                kwargs.pop("use_cache", None)
                return fn(*args, **kwargs)
            ttl = _ttl_for(domain)
            key = _mk_key(domain, args, kwargs)
            hit = _cache_get(key, ttl)
            if hit is not None:
                _CACHE_HITS[domain] = _CACHE_HITS.get(domain, 0) + 1
                return hit
            _CACHE_MISS[domain] = _CACHE_MISS.get(domain, 0) + 1
            val = fn(*args, **kwargs)
            if _nonempty(val):
                _cache_put(key, val, ttl)
            return val
        return wrap
    return deco


def cache_clear(domain: str = None) -> dict:
    """清缓存。domain=None 清全部域;否则只清该域。返回 {mem_cleared, file_cleared}。"""
    n_mem = n_file = 0
    with _MEM_LOCK:
        for k in [k for k in _MEM if domain is None or k.startswith(domain + ":")]:
            _MEM.pop(k, None)
            n_mem += 1
    try:
        import shutil
        roots = ([_os.path.join(_CACHE_DIR, d) for d in _os.listdir(_CACHE_DIR)]
                 if domain is None and _os.path.isdir(_CACHE_DIR)
                 else [_os.path.join(_CACHE_DIR, domain)])
        for p in roots:
            if _os.path.isdir(p):
                n_file += len([x for x in _os.listdir(p) if x.endswith(".pkl")])
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass
    try:
        from core import cache as _rc
        c = _rc._redis()
        if c:
            pat = _rc._NS + "dh:" + (domain + ":*" if domain else "*")
            for k in c.scan_iter(pat):
                c.delete(k)
    except Exception:
        pass
    return {"mem_cleared": n_mem, "file_cleared": n_file}


def cache_stats() -> dict:
    """缓存观测:开关/内存条目数/Redis 是否在线/各域命中与未命中。"""
    try:
        from core import cache as _rc
        redis_up = _rc.available()
    except Exception:
        redis_up = False
    with _MEM_LOCK:
        mem_n = len(_MEM)
    doms = sorted(set(_CACHE_HITS) | set(_CACHE_MISS))
    return {
        "enabled": _CACHE_ON,
        "redis": redis_up,
        "mem_entries": mem_n,
        "cache_dir": _CACHE_DIR,
        "by_domain": {d: {"hit": _CACHE_HITS.get(d, 0), "miss": _CACHE_MISS.get(d, 0)} for d in doms},
    }


def _norm_code(code: str) -> str:
    """规整 A 股代码为 6 位(去 sh/sz/bj 前缀、补零)。非数字代码原样返回。"""
    c = str(code).strip().lower()
    for p in ('sh', 'sz', 'bj'):
        if c.startswith(p):
            c = c[len(p):]
            break
    return c.zfill(6) if c.isdigit() and len(c) <= 6 else str(code).strip()


# 懒加载底层源(避免 import 期连锁加载/循环依赖)
_ADAPTER = None
_FETCHER = None


def _adapter():
    global _ADAPTER
    if _ADAPTER is None:
        from a_stock_data_adapter import adapter
        _ADAPTER = adapter
    return _ADAPTER


def _fetcher():
    global _FETCHER
    if _FETCHER is None:
        from stock_data import StockDataFetcher
        _FETCHER = StockDataFetcher()
    return _FETCHER


# ══════════════════════════════════════════════════════════
#  行情域:实时报价 / 个股信息 / K线 / 指数
# ══════════════════════════════════════════════════════════

def quotes(codes: List[str]) -> Dict[str, dict]:
    """批量实时行情。返回 {code(6位): 标准quote dict}。源:腾讯主→东财ulist(adapter 内置兜底)。"""
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {}
    raw = _route("quotes", [("a_stock", lambda: _adapter().get_quotes(codes))], empty={}) or {}
    return {_norm_code(k): v for k, v in raw.items()}


def quote(code: str) -> dict:
    """单只实时行情(标准 quote dict)。"""
    return quotes([code]).get(_norm_code(code), {})


# ── K线磁盘缓存:日线日内不变,回测/因子/优化器反复拉同一批 → 缓存共享大幅提速 ──
# (_os 已在上方统一缓存层导入;此处自带专用缓存,不走通用 _dh_cache)
_KLINE_DIR = _os.path.join(_bootstrap.DB_DIR, "kline_cache")


def _kline_ttl() -> int:
    """缓存有效期(秒):盘中 9:00-15:30 用 1h(今日 bar 会变),其余时段 12h。"""
    try:
        from datetime import datetime as _dt
        m = _dt.now().hour * 60 + _dt.now().minute
        return 3600 if (9 * 60 <= m <= 15 * 60 + 30) else 43200
    except Exception:
        return 3600


def _sanitize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """K线数据质量护栏:丢弃收盘价 NaN / 非正(<=0)的脏行(会污染回测/因子/指标)。
    只清明确无效的行,不动复权跳变等正常波动。列名兼容大小写。"""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        close_col = "Close" if "Close" in df.columns else ("close" if "close" in df.columns else None)
        if close_col is None:
            return df
        c = pd.to_numeric(df[close_col], errors="coerce")
        good = c.notna() & (c > 0)
        return df[good] if not good.all() else df
    except Exception:
        return df


def kline(code: str, period: str = "1y", interval: str = "1d", use_cache: bool = True) -> pd.DataFrame:
    """K线 DataFrame(列 date/open/high/low/close/volume/p_change)。
    内部走 StockDataFetcher(已多源:东财/akshare/Ashare/mootdx);磁盘缓存日线提速。失败返回空 DF。
    use_cache=False 强制实时拉(需要今日最新 bar 时用)。"""
    cache_f = _os.path.join(_KLINE_DIR, f"{_norm_code(code)}_{period}_{interval}.pkl")
    if use_cache:
        try:
            if _os.path.isfile(cache_f) and (_time.time() - _os.path.getmtime(cache_f)) < _kline_ttl():
                df = pd.read_pickle(cache_f)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df
        except Exception:
            pass

    def _f():
        df = _fetcher().get_stock_data(code, period, interval)
        return pd.DataFrame() if isinstance(df, dict) else (df if isinstance(df, pd.DataFrame) else pd.DataFrame())
    df = _route("kline", [("fetcher", _f)], empty=pd.DataFrame())
    df = _sanitize_kline(df)
    if use_cache and isinstance(df, pd.DataFrame) and not df.empty:
        try:
            _os.makedirs(_KLINE_DIR, exist_ok=True)
            df.to_pickle(cache_f)
        except Exception:
            pass
    return df


# ── K线缓存预热(每日盘后焐热目标股票池,供回测/因子/晨报/持仓守卫命中暖缓存)──
# 实测主源(新浪日K)无视 period、每次返回完整 ~365 根**已按当日复权因子重算**的日线序列。
# 故"全量拉一次"既廉价(单次HTTP~88ms)又天然无复权漂移 → 不需增量追加/锚点校验/周末特判。
# 主拉 1y 写缓存,6mo/1mo 由切片派生(零额外外部调用),命名与 kline() 读路径一致即被其命中。
_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095}


def _period_days(period: str) -> int:
    return _PERIOD_DAYS.get(period, 365)


def _slice_by_days(df: pd.DataFrame, days: int) -> pd.DataFrame:
    """按 DatetimeIndex 截取最近 days 自然日(保留索引与列结构不变)。"""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        from datetime import datetime as _dt, timedelta as _td
        cutoff = pd.Timestamp(_dt.now() - _td(days=days))
        return df[df.index >= cutoff]
    except Exception:
        return df


def _write_kline_cache(code: str, period: str, interval: str, df: pd.DataFrame) -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False
    try:
        _os.makedirs(_KLINE_DIR, exist_ok=True)
        df.to_pickle(_os.path.join(_KLINE_DIR, f"{_norm_code(code)}_{period}_{interval}.pkl"))
        return True
    except Exception:
        return False


def prefetch_kline(code: str, periods=("1y", "6mo", "1mo"), interval: str = "1d") -> dict:
    """预拉一只股票的 K线并写入各 period 磁盘缓存(全量,天然无复权漂移)。
    主拉最长 period 一次,其余 period 由切片派生(不再额外请求外部源)。
    返回 {code, bars, wrote}(bars=主序列条数,wrote=写入的缓存文件数)。失败 bars=0。"""
    base = max(periods, key=_period_days) if periods else "1y"

    def _f():
        d = _fetcher().get_stock_data(code, base, interval)
        return pd.DataFrame() if isinstance(d, dict) else (d if isinstance(d, pd.DataFrame) else pd.DataFrame())
    df = _route("kline", [("fetcher", _f)], empty=pd.DataFrame())
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {"code": code, "bars": 0, "wrote": 0}
    wrote = 0
    for p in periods:
        sub = df if p == base else _slice_by_days(df, _period_days(p))
        if _write_kline_cache(code, p, interval, sub):
            wrote += 1
    return {"code": code, "bars": int(len(df)), "wrote": wrote}


def kline_with_indicators(code: str, period: str = "1y") -> pd.DataFrame:
    """K线 + MyTT 技术指标(MA/MACD/KDJ/RSI/BOLL...)。失败返回空 DF。"""
    f = _fetcher()
    df = f.get_stock_data(code, period)
    if isinstance(df, dict) or df is None or len(df) == 0:
        return pd.DataFrame()
    ind = f.calculate_technical_indicators(df)
    return pd.DataFrame() if isinstance(ind, dict) else ind


def stock_info(code: str) -> dict:
    """个股基础信息(名称/价格/PE/PB/市值/行业...)。走 StockDataFetcher(多源兜底)。"""
    info = _fetcher().get_stock_info(code)
    return info if isinstance(info, dict) else {}


_INDEX_SPECS = [("上证指数", "s_sh000001", "s_sh000001"), ("深证成指", "s_sz399001", "s_sz399001"),
                ("创业板指", "s_sz399006", "s_sz399006"), ("科创50", "s_sh000688", "s_sh000688"),
                ("沪深300", "s_sh000300", "s_sh000300"), ("恒生指数", "rt_hkHSI", "r_hkHSI")]


def _indices_sina() -> List[dict]:
    import urllib.request
    url = "https://hq.sinajs.cn/list=" + ",".join(s for _, s, _ in _INDEX_SPECS)
    txt = urllib.request.urlopen(urllib.request.Request(
        url, headers={"Referer": "https://finance.sina.com.cn"}), timeout=8).read().decode("gb2312", "replace")
    raw = {line.split("=", 1)[0].replace("var", "").strip()[7:]: line.split('"', 2)[1].split(",")
           for line in txt.splitlines() if "hq_str_" in line and '="' in line}
    out = []
    for name, ssym, _ in _INDEX_SPECS:
        v = raw.get(ssym)
        if not v:
            continue
        try:
            if ssym.startswith("rt_hk"):
                out.append({"name": name, "value": float(v[6]), "change_amt": float(v[7]), "change_pct": float(v[8])})
            else:
                out.append({"name": name, "value": float(v[1]), "change_amt": float(v[2]), "change_pct": float(v[3])})
        except Exception:
            continue
    return out


def _indices_tencent() -> List[dict]:
    import urllib.request
    url = "https://qt.gtimg.cn/q=" + ",".join(t for _, _, t in _INDEX_SPECS)
    txt = urllib.request.urlopen(urllib.request.Request(
        url, headers={"Referer": "https://finance.qq.com"}), timeout=8).read().decode("gbk", "replace")
    raw = {line.split("=", 1)[0].replace("v_", "").strip(): line.split('"', 2)[1].split("~")
           for line in txt.splitlines() if line.startswith("v_") and '="' in line}
    out = []
    for name, _, tsym in _INDEX_SPECS:
        v = raw.get(tsym)
        if not v or len(v) < 6:
            continue
        try:
            cur = float(v[3])
            if tsym.startswith("r_"):
                prev = float(v[4]); amt = cur - prev; pct = (amt / prev * 100) if prev else 0
            else:
                amt = float(v[4]); pct = float(v[5])
            out.append({"name": name, "value": cur, "change_amt": amt, "change_pct": pct})
        except Exception:
            continue
    return out


def indices() -> List[dict]:
    """主要大盘指数实时。list[dict] 键:name/value/change_amt/change_pct。源链:新浪 / 腾讯(自动升降级)。"""
    return _route("indices", [("sina", _indices_sina), ("tencent", _indices_tencent)], empty=[]) or []


# ══════════════════════════════════════════════════════════
#  资金域:北向 / 个股资金流 / 龙虎榜 / 融资融券
# ══════════════════════════════════════════════════════════

def north_flow(days: int = 30) -> List[dict]:
    """北向资金近 N 日。list[dict] 键:trade_date/hgt_yi/sgt_yi/net_total...
    源:北向本地缓存(同花顺,读时自刷新)+ adata(走 data_source_manager 既有链)。"""
    from data_source_manager import data_source_manager as dsm
    return _route("north_flow", [("dsm", lambda: dsm.get_north_flow_a_data(days))], empty=[]) or []


def capital_flow(code: str, days: int = 120) -> List[dict]:
    """个股历史资金流(日级)。list[dict]。源:a_stock_adapter(东财非push2)。"""
    return _route("capital_flow", [("a_stock", lambda: _adapter().get_fund_flow_history(code, days))], empty=[]) or []


def capital_flow_minute(code: str) -> List[dict]:
    """个股当日分钟级资金流。list[dict]。"""
    return _route("capital_flow_minute", [("a_stock", lambda: _adapter().get_fund_flow_minute(code))], empty=[]) or []


def dragon_tiger(trade_date: str = None) -> List[dict]:
    """全市场龙虎榜明细(指定/最近交易日)。list[dict]。"""
    from data_source_manager import data_source_manager as dsm
    return _route("dragon_tiger", [("dsm", lambda: dsm.get_dragon_tiger_detail_a_data(trade_date))], empty=[]) or []


def dragon_tiger_stock(code: str, trade_date: str = None, look_back: int = 30) -> dict:
    """个股龙虎榜上榜记录。dict。"""
    return _route("dragon_tiger_stock",
                  [("a_stock", lambda: _adapter().get_dragon_tiger(code, trade_date, look_back))], empty={}) or {}


def margin(code: str, page_size: int = 30) -> List[dict]:
    """个股融资融券明细。list[dict]。"""
    return _route("margin", [("a_stock", lambda: _adapter().get_margin_trading(code, page_size))], empty=[]) or []


def block_trade(code: str, page_size: int = 20) -> List[dict]:
    """个股大宗交易。list[dict]。"""
    return _route("block_trade", [("a_stock", lambda: _adapter().get_block_trade(code, page_size))], empty=[]) or []


def holder_num_change(code: str, page_size: int = 10) -> List[dict]:
    """股东户数变化。list[dict]。"""
    return _route("holder_num_change", [("a_stock", lambda: _adapter().get_holder_num_change(code, page_size))], empty=[]) or []


def dividend_history(code: str, page_size: int = 20) -> List[dict]:
    """分红送转历史。list[dict]。"""
    return _route("dividend_history", [("a_stock", lambda: _adapter().get_dividend_history(code, page_size))], empty=[]) or []


def lockup_expiry(code: str, trade_date: str = None, forward_days: int = 90) -> dict:
    """限售解禁。dict。"""
    return _route("lockup_expiry", [("a_stock", lambda: _adapter().get_lockup_expiry(code, trade_date, forward_days))], empty={}) or {}


# ══════════════════════════════════════════════════════════
#  板块/题材域
# ══════════════════════════════════════════════════════════

def hot_stocks(date: str = None) -> pd.DataFrame:
    """当日强势股 + 题材归因(DataFrame)。源:同花顺热点。"""
    return _route("hot_stocks", [("ths", lambda: _adapter().get_hot_stocks(date))], empty=pd.DataFrame())


def sector_ranking(sector_type: str = "industry", top_n: int = 20) -> dict:
    """板块涨跌排名 {top,bottom,total}。sector_type: industry/concept。源:东财 push2(非鉴权)。"""
    a = _adapter()
    fn = a.get_concept_ranking if sector_type == "concept" else a.get_industry_ranking
    return _route(f"sector_ranking_{sector_type}", [("em_push2", lambda: fn(top_n))],
                  empty={"top": [], "bottom": [], "total": 0}) or {"top": [], "bottom": [], "total": 0}


def sector_spot(top_n: int = 8, bottom_n: int = 5) -> dict:
    """新浪行业板块快照(涨跌排序)。返回 {top:[{板块,涨跌幅,领涨}], bottom:[...]}。源:akshare 新浪行业。"""
    def _sina_ak():
        import akshare as ak
        df = ak.stock_sector_spot(indicator="新浪行业")
        return sorted([{"板块": r.get("板块"), "涨跌幅": round(float(r.get("涨跌幅") or 0), 2),
                        "领涨": r.get("股票名称")} for _, r in df.iterrows()],
                      key=lambda x: x["涨跌幅"], reverse=True)

    rows = _route("sector_spot", [("sina_ak", _sina_ak)], empty=[]) or []
    return {"top": rows[:top_n], "bottom": rows[-bottom_n:]} if rows else {"top": [], "bottom": []}


def sector_fund_flow(sector_type: str = "industry", top_n: int = 50) -> List[dict]:
    """板块资金流(行业/概念)。list[dict]。"""
    return _route(f"sector_fund_flow_{sector_type}",
                  [("a_stock", lambda: _adapter().get_sector_fund_flow(sector_type, top_n))], empty=[]) or []


def concept_blocks(code: str) -> dict:
    """个股概念/行业/地域归属。dict 键:industry/concept/region/concept_tags。"""
    _empty = {"industry": [], "concept": [], "region": [], "concept_tags": []}
    return _route("concept_blocks", [("a_stock", lambda: _adapter().get_concept_blocks(code))], empty=_empty) or _empty


# ══════════════════════════════════════════════════════════
#  基本面/估值域
# ══════════════════════════════════════════════════════════

def financials(code: str, report_type: str = "lrb") -> List[dict]:
    """财报三表。report_type: fzb 资产负债 / lrb 利润 / llb 现金流。list[dict]。"""
    return _route("financials", [("a_stock", lambda: _adapter().get_financial_reports(code, report_type))], empty=[]) or []


def valuation(code: str) -> dict:
    """估值(PE/PB/市值等)。dict。源:adapter.full_valuation。"""
    return _route("valuation", [("a_stock", lambda: _adapter().full_valuation(code))], empty={}) or {}


def full_valuation(code: str) -> dict:
    """完整估值(前向PE/PEG/PE消化年数)。dict。"""
    return _route("full_valuation", [("a_stock", lambda: _adapter().get_full_valuation(code))], empty={}) or {}


def eps_forecast(code: str) -> pd.DataFrame:
    """机构一致预期 EPS(DataFrame)。"""
    return _route("eps_forecast", [("ths", lambda: _adapter().get_eps_forecast(code))], empty=pd.DataFrame())


# ══════════════════════════════════════════════════════════
#  新闻/公告域
# ══════════════════════════════════════════════════════════

def stock_news(code: str, page_size: int = 20) -> List[dict]:
    """个股新闻。list[dict] 键:title/time/url/...。"""
    from data_source_manager import data_source_manager as dsm
    return _route("stock_news", [("dsm", lambda: dsm.get_stock_news_a_stock(code, page_size))], empty=[]) or []


def _news_em(page_size):
    import akshare as ak
    df = ak.stock_info_global_em()
    if df is None or df.empty:
        return []
    return [{"title": str(r.get("标题", "")), "content": str(r.get("摘要", "")),
             "time": str(r.get("发布时间", "")), "url": str(r.get("链接", ""))}
            for _, r in df.head(page_size).iterrows()]


def _news_cls(page_size):
    from data_source_manager import data_source_manager as dsm
    return [{"title": n.get("title") or (n.get("content", "")[:50]),
             "content": n.get("content") or n.get("summary", ""),
             "time": n.get("time") or n.get("ctime", ""), "url": n.get("url", "")}
            for n in (dsm.get_market_news_a_stock(page_size=page_size) or [])]


def market_news(page_size: int = 50) -> List[dict]:
    """全市场财经快讯。list[dict] 键:title/content/time/url。
    源链:东财全球快讯(akshare) / 财联社电报(dsm)—— 自动升降级。"""
    return _route("market_news",
                  [("em", lambda: _news_em(page_size)), ("cls", lambda: _news_cls(page_size))], empty=[]) or []


def announcements(code: str) -> List[dict]:
    """个股公告。list[dict]。"""
    from data_source_manager import data_source_manager as dsm
    return _route("announcements", [("dsm", lambda: dsm.get_announcements_a_stock(code))], empty=[]) or []


# ══════════════════════════════════════════════════════════
#  选股域(条件筛选)
# ══════════════════════════════════════════════════════════

def screen(price_max=None, pe_max=None, profit_growth_min=None,
           mcap_max=None, mcap_min=None, top_n=10,
           sort_field="f3", sort_asc=False, include_kcb=False) -> dict:
    """条件选股(统一入口)。走 selection.data_source_config.screen_stocks(已 auto/push2/pywencai/dataapi 多源)。
    返回 {success, data:[{code,name,price,pe,growth,mcap,pb}], msg}。"""
    try:
        from selection.data_source_config import screen_stocks
        return screen_stocks(price_max=price_max, pe_max=pe_max,
                             profit_growth_min=profit_growth_min,
                             mcap_max=mcap_max, mcap_min=mcap_min, top_n=top_n,
                             sort_field=sort_field, sort_asc=sort_asc, include_kcb=include_kcb)
    except Exception as e:
        return {"success": False, "data": [], "msg": str(e)}


# ══════════════════════════════════════════════════════════
#  可转债域(双低策略)
# ══════════════════════════════════════════════════════════
def _cb_num(v):
    try:
        f = float(v)
        return round(f, 3) if f == f else None   # NaN→None
    except Exception:
        return None


def _cb_jsl() -> List[dict]:
    import akshare as ak
    df = ak.bond_cb_jsl()
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, r in df.iterrows():
        price = _cb_num(r.get('现价'))
        prem = _cb_num(r.get('转股溢价率'))
        dl = _cb_num(r.get('双低'))
        if dl is None and price is not None and prem is not None:
            dl = round(price + prem, 2)
        out.append({
            'code': str(r.get('代码', '')), 'name': str(r.get('转债名称', '')),
            'price': price, 'change_pct': _cb_num(r.get('涨跌幅')),
            'premium_pct': prem, 'conv_value': _cb_num(r.get('转股价值')),
            'double_low': dl, 'rating': str(r.get('债券评级', '') or ''),
            'stock_code': str(r.get('正股代码', '')), 'stock_name': str(r.get('正股名称', '')),
            'ytm_pct': _cb_num(r.get('到期税前收益')), 'remain_years': _cb_num(r.get('剩余年限')),
            'remain_scale_yi': _cb_num(r.get('剩余规模')), 'turnover_pct': _cb_num(r.get('换手率')),
        })
    return out


def _cb_eastmoney() -> List[dict]:
    import akshare as ak
    df = ak.bond_cov_comparison()
    if df is None or getattr(df, "empty", True):
        return []
    cols = list(df.columns)

    def pick(r, *names):
        for n in names:
            for c in cols:
                if n in c:
                    return r.get(c)
        return None

    out = []
    for _, r in df.iterrows():
        price = _cb_num(pick(r, '转债最新价', '转债现价', '最新价'))
        prem = _cb_num(pick(r, '转股溢价率'))
        out.append({
            'code': str(pick(r, '转债代码', '代码') or ''), 'name': str(pick(r, '转债名称', '名称') or ''),
            'price': price, 'change_pct': _cb_num(pick(r, '转债涨跌幅', '涨跌幅')),
            'premium_pct': prem, 'conv_value': _cb_num(pick(r, '转股价值')),
            'double_low': round(price + prem, 2) if (price is not None and prem is not None) else None,
            'rating': str(pick(r, '债券评级', '评级') or ''), 'stock_code': str(pick(r, '正股代码') or ''),
            'stock_name': str(pick(r, '正股名称') or ''), 'ytm_pct': _cb_num(pick(r, '到期收益率', '纯债收益率')),
            'remain_years': _cb_num(pick(r, '剩余年限')), 'remain_scale_yi': _cb_num(pick(r, '剩余规模')),
            'turnover_pct': _cb_num(pick(r, '换手率')),
        })
    return out


def convertible_bonds() -> List[dict]:
    """全市场可转债比价(双低策略用)。list[dict] 键:code/name/price/change_pct/premium_pct(转股溢价率)/
    conv_value(转股价值)/double_low(双低=价+溢价率)/rating(评级)/stock_code/stock_name/ytm_pct/remain_years/
    remain_scale_yi(剩余规模亿)/turnover_pct。源链:东财比价表(全市场,生产可达)→ 集思录(各处可达,匿名约30只)。"""
    return _route("convertible_bonds",
                  [("eastmoney", _cb_eastmoney), ("jsl", _cb_jsl)], empty=[]) or []


# ── 基金历史净值 ──────────────────────────────────────────────────────
# 主源:东财官方 lsjz JSON(纯 HTTP, 无 JS exec, 稳定)
# 兜底:akshare fund_open_fund_info_em(部分基金 PyExecJS 抛 ReferenceError 时主源已覆盖)
# 返回标准列 [date, unit_nav, acc_nav, daily_return] 升序 DataFrame。
def _fund_nav_eastmoney(code: str) -> pd.DataFrame:
    """直连东财 f10/lsjz JSON 翻页拉取全部历史净值。被 _route 调用, 失败抛异常即可。"""
    import urllib.request
    import json as _json
    try:
        from rate_limiter import throttle as _throttle
    except Exception:
        def _throttle(*a, **k): return 0.0
    code = str(code).zfill(6)
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': f'http://fundf10.eastmoney.com/jjjz_{code}.html',
    }
    rows: List[dict] = []
    page_size = 200
    for page in range(1, 200):  # 200*200=40000 条上限, 远超任何基金的历史长度
        url = (f'https://api.fund.eastmoney.com/f10/lsjz?'
               f'fundCode={code}&pageIndex={page}&pageSize={page_size}')
        _throttle('eastmoney')
        req = urllib.request.Request(url, headers=headers)
        raw = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', 'replace')
        data = _json.loads(raw)
        lst = ((data.get('Data') or {}).get('LSJZList')) or []
        if not lst:
            break
        rows.extend(lst)
        if len(lst) < page_size:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        'FSRQ': 'date', 'DWJZ': 'unit_nav', 'LJJZ': 'acc_nav', 'JZZZL': 'daily_return',
    })
    keep = [c for c in ('date', 'unit_nav', 'acc_nav', 'daily_return') if c in df.columns]
    if 'date' not in keep or 'unit_nav' not in keep:
        return pd.DataFrame()
    df = df[keep].copy()
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    for c in ('unit_nav', 'acc_nav', 'daily_return'):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    if 'acc_nav' not in df.columns:
        df['acc_nav'] = df['unit_nav']
    return df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)


def _fund_nav_akshare(code: str) -> pd.DataFrame:
    """akshare 兜底。被 _route 调用, 异常会被 _route 吞掉续试下一源,
    所以这里不用自己 print 长 traceback; 只在内部捕一次 acc_nav 失败(unit 已有则继续)。"""
    try:
        from rate_limiter import throttle as _throttle
    except Exception:
        def _throttle(*a, **k): return 0.0
    import akshare as ak    # 缺包时让 _route 捕异常自动跳过
    code = str(code).zfill(6)
    _throttle('akshare')
    unit = ak.fund_open_fund_info_em(symbol=code, indicator='单位净值走势')
    if unit is None or len(unit) == 0:
        return pd.DataFrame()
    unit = unit.rename(columns={'净值日期': 'date', '单位净值': 'unit_nav', '日增长率': 'daily_return'})
    df = unit[[c for c in ['date', 'unit_nav', 'daily_return'] if c in unit.columns]].copy()
    if 'date' not in df.columns or 'unit_nav' not in df.columns:
        return pd.DataFrame()
    try:
        _throttle('akshare')
        acc = ak.fund_open_fund_info_em(symbol=code, indicator='累计净值走势')
        if acc is not None and len(acc):
            acc = acc.rename(columns={'净值日期': 'date', '累计净值': 'acc_nav'})
            df = df.merge(acc[['date', 'acc_nav']], on='date', how='left')
    except Exception:
        # 累计净值 nice-to-have; unit 已在手, 静默
        pass
    if 'acc_nav' not in df.columns:
        df['acc_nav'] = df['unit_nav']
    df['date'] = pd.to_datetime(df['date'])
    for c in ('unit_nav', 'acc_nav', 'daily_return'):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.sort_values('date').reset_index(drop=True)


def fund_nav_history(code: str, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
    """基金历史净值。源链:东财 lsjz JSON → akshare PyExecJS 兜底。
    返回 DataFrame[date, unit_nav, acc_nav, daily_return] 升序;两源都失败返回 None。
    start/end='YYYY-MM-DD' 区间过滤。

    缓存:'fund_nav' 域(见 _CACHE_TTL);start/end 也参与缓存 key, 不会互相覆盖。
    """
    code = str(code).zfill(6)
    df = _route("fund_nav",
                [("eastmoney", lambda: _fund_nav_eastmoney(code)),
                 ("akshare",   lambda: _fund_nav_akshare(code))],
                empty=pd.DataFrame())
    if df is None or (hasattr(df, 'empty') and df.empty):
        return None
    if start:
        df = df[df['date'] >= pd.to_datetime(start)]
    if end:
        df = df[df['date'] <= pd.to_datetime(end)]
    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════
#  给非实时数据域统一套缓存(在门面定义前完成,使 hub.* 与内部互调都走缓存版)
#  · 不含 quote(委托 quotes,自动受益)、kline(自带专用磁盘缓存)。
#  · 各域 TTL 见 _CACHE_TTL;实时域(quotes/indices/capital_flow_minute)盘中秒级。
# ══════════════════════════════════════════════════════════
for _name in ("quotes", "indices", "capital_flow_minute", "kline_with_indicators",
              "stock_info", "north_flow", "capital_flow", "dragon_tiger", "dragon_tiger_stock",
              "margin", "block_trade", "holder_num_change", "dividend_history", "lockup_expiry",
              "hot_stocks", "sector_ranking", "sector_spot", "sector_fund_flow", "concept_blocks",
              "financials", "valuation", "full_valuation", "eps_forecast",
              "stock_news", "market_news", "announcements", "screen", "convertible_bonds",
              "fund_nav_history"):
    globals()[_name] = _dh_cache(_name if _name != "fund_nav_history" else "fund_nav")(globals()[_name])
del _name


# ══════════════════════════════════════════════════════════
#  门面对象(也可 `from datahub import hub; hub.quotes(...)`)
# ══════════════════════════════════════════════════════════

class _Hub:
    quotes = staticmethod(quotes)
    quote = staticmethod(quote)
    kline = staticmethod(kline)
    prefetch_kline = staticmethod(prefetch_kline)
    kline_with_indicators = staticmethod(kline_with_indicators)
    stock_info = staticmethod(stock_info)
    indices = staticmethod(indices)
    north_flow = staticmethod(north_flow)
    capital_flow = staticmethod(capital_flow)
    capital_flow_minute = staticmethod(capital_flow_minute)
    dragon_tiger = staticmethod(dragon_tiger)
    dragon_tiger_stock = staticmethod(dragon_tiger_stock)
    margin = staticmethod(margin)
    block_trade = staticmethod(block_trade)
    holder_num_change = staticmethod(holder_num_change)
    dividend_history = staticmethod(dividend_history)
    lockup_expiry = staticmethod(lockup_expiry)
    hot_stocks = staticmethod(hot_stocks)
    sector_ranking = staticmethod(sector_ranking)
    sector_spot = staticmethod(sector_spot)
    sector_fund_flow = staticmethod(sector_fund_flow)
    concept_blocks = staticmethod(concept_blocks)
    financials = staticmethod(financials)
    valuation = staticmethod(valuation)
    full_valuation = staticmethod(full_valuation)
    eps_forecast = staticmethod(eps_forecast)
    stock_news = staticmethod(stock_news)
    market_news = staticmethod(market_news)
    announcements = staticmethod(announcements)
    screen = staticmethod(screen)
    convertible_bonds = staticmethod(convertible_bonds)
    fund_nav_history = staticmethod(fund_nav_history)
    source_stats = staticmethod(source_stats)
    cache_stats = staticmethod(cache_stats)
    cache_clear = staticmethod(cache_clear)


hub = _Hub()


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("=== datahub 统一数据层自检(含自适应路由 + 三级缓存)===")
    print(f"quote(600519): {quote('600519').get('name')} 价 {quote('600519').get('price')}")
    print(f"indices: {[x['name'] for x in indices()]}")
    print(f"market_news(5): {len(market_news(5))} 条")

    # 缓存验证:同一调用第二次应命中缓存(秒回)
    t0 = _time.time(); indices(); cold = _time.time() - t0
    t0 = _time.time(); indices(); warm = _time.time() - t0
    print(f"\nindices 冷算 {cold*1000:.0f}ms → 缓存命中 {warm*1000:.1f}ms")
    print(f"use_cache=False 强制实时:{len(indices(use_cache=False))} 个指数")

    print("\n源统计 source_stats():")
    for k, v in source_stats().items():
        print(f"  {k}: ok={v['ok']} fail={v['fail']} rate={v['rate']} avg={v['avg_ms']}ms cooling={v['cooling']}")
    cs = cache_stats()
    print(f"\n缓存 cache_stats(): 开关={cs['enabled']} Redis={cs['redis']} 内存条目={cs['mem_entries']}")
    for d, hm in cs["by_domain"].items():
        print(f"  {d}: hit={hm['hit']} miss={hm['miss']}")
    print("OK")
