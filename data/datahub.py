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


_UNKNOWN_SCORE = 0.5   # 未知/未试过的源评分(中性)


def _health(key: str, now: float) -> float:
    """源健康度评分(越高越优先)。
    ⚠️ 2026-06-24 关键修正:未知源给 0.5(中性), **不再给 1.0**。
    原先未知源=1.0 会盖过"已验证健康"的源(rate=1.0 但有延迟惩罚 → 仅 0.97),导致
    新加的源一上来就抢主位。正好踩上"新源把限流接口(东财)打到 IP 被封"的雷。
    改 0.5 后:已验证健康源(>0.5)稳居主位、未试源仅作兜底、连续失败源(冷却 -1.0)沉底,
    且静态源链顺序(列在前的)在同分时通过 stable sort 生效 —— 手动把可达源列前真正起作用。
    成功率为主导,平均延迟轻微扣分;连续失败且在冷却期 → 重罚沉底。"""
    s = _STATS.get(key)
    if s is None:
        return _UNKNOWN_SCORE
    total = s.ok + s.fail
    if total == 0:
        return _UNKNOWN_SCORE
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


import os as _os_route
import concurrent.futures as _cf

# ⭐ 每个数据源的硬超时(秒, 2026-06-23): 这是全项目外部数据的总闸门。
# 任何源(腾讯/东财/新浪/akshare/...)的单次取数超过这个时间, 一律当失败切断 → 试下一个源,
# 都没有就返回 empty。彻底根治"外部接口卡死拖垮整个任务"(quotes/kline/资金流 等无差别覆盖)。
# 可用 env DATAHUB_SOURCE_TIMEOUT 调整。
_SOURCE_TIMEOUT = int(_os_route.getenv("DATAHUB_SOURCE_TIMEOUT", "20"))
# 独立线程池跑源调用; 不用 with(__exit__ 会 wait 卡死的孤儿线程)。
# ⭐ max_workers 给足(2026-06-24: 16→64): 外网整体抽风时, 死源每次挂满 timeout 秒,
# 选股因子循环(60 只)+ 多监控任务并发会把池占满 → 后续 submit 排队也被 result(timeout)
# 算成"假超时"级联。给到 64, 让"线程多到不排队"(线程只是 IO 等待, 廉价)。
_ROUTE_POOL = _cf.ThreadPoolExecutor(max_workers=64, thread_name_prefix="datahub-route")
# 超时日志限频: 同一 (域:源) 60s 内最多打一条, 避免外网全挂时刷几百行。
_TO_LOG_LAST: Dict[str, float] = {}
_TO_LOG_GAP = 60.0


def _route(capability: str, sources: List[Tuple[str, Callable[[], Any]]], empty=None,
           timeout: int = None):
    """按健康度动态排序源链,依次试,返回第一个"非空"结果并记录统计(供自动升降级)。
    sources: [(源名, 无参thunk), ...]。DataFrame 用 not empty 判空,其余用 truthy。
    ⭐ 每源套硬超时(默认 _SOURCE_TIMEOUT): 源卡死 timeout 秒后强制当失败 → 试下一个源,
       不会无限等(根治 datahub.quotes 等卡死拖垮 jobs)。单源异常/超时被吞续试下一个。"""
    to = timeout or _SOURCE_TIMEOUT
    now = _time.time()
    # ⚡ 全源熔断(2026-06-25):该域所有源都在活跃冷却期(连续失败沉底,score<-0.5)→ 外网/该域整体
    # 不可达,直接返回 empty,不再逐源吃满超时。外网全挂时让 监控/选股/快照 等批量任务秒级降级而非
    # 每只吃 quotes60s+kline135s 拖到任务超时(1813s 加仓审核即此)。冷却 120s 后自动放行重试 → 自愈。
    if sources and all(_health(f"{capability}:{n}", now) < -0.5 for n, _ in sources):
        return empty
    ordered = sorted(sources, key=lambda ns: -_health(f"{capability}:{ns[0]}", now))
    for name, fn in ordered:
        key = f"{capability}:{name}"
        t0 = _time.time()
        try:
            fut = _ROUTE_POOL.submit(fn)
            v = fut.result(timeout=to)
        except _cf.TimeoutError:
            fut.cancel()  # 孤儿线程留底层自然结束, 不阻塞
            _record(key, False, _time.time() - t0)
            _t = _time.time()
            if _t - _TO_LOG_LAST.get(key, 0) >= _TO_LOG_GAP:
                _TO_LOG_LAST[key] = _t
                print(f"[datahub] ⏱️ {key} 源超时 {to}s, 切下一个源(60s 内同源仅提示一次)", flush=True)
            continue
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
    # 估值/一致预期:EPS 是券商一致预期,几天才变,price 略旧对 PEG/前向PE 因子无所谓。
    # (2026-06-25)盘中也拉到 1 天:原盘中 1h → 09:45 选股 60 只候选缓存过期、逐只现调同花顺慢源,
    # 是池耗尽雪崩的一半。盘后 kline_prefetch 顺便焐热,盘中读缓存 0 调同花顺。
    "valuation": 86400,
    "full_valuation": 86400,
    "eps_forecast": 86400,
    "stock_reports": 86400,
    "industry_reports": 86400,
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
    """批量实时行情。返回 {code(6位): 标准quote dict}。
    datahub 级并列三源(2026-06-24,根治"腾讯卡满超时被砍→东财兜底没轮到→全空"):
      - a_stock:腾讯主 + 东财补缺(逐代码互补,处理"腾讯偶尔缺几只冷门票")
      - eastmoney:纯东财 ulist(腾讯整体卡死/被 _route 砍掉时的独立兜底,独立超时+健康度记账)
      - sina:纯新浪 hq(真·独立源,非东财非腾讯;腾讯+东财同公司都挂时的最后底。
        只有行情无 PE/PB/市值,这些字段 0,但 name/价/涨跌核心字段保住,报告不至全空)
    _route 按健康度排序:某源连续卡死会降级,其余自动上位,不再让单点拖垮取数。"""
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {}
    raw = _route("quotes",
                 [("a_stock", lambda: _adapter().get_quotes(codes)),
                  ("eastmoney", lambda: _adapter().get_quotes_eastmoney(codes)),
                  ("sina", lambda: _adapter().get_quotes_sina(codes))],
                 empty={}) or {}
    norm = {_norm_code(k): v for k, v in raw.items()}
    _name_remember(norm)   # 顺带把中文名焐进持久缓存(见下:行情源挂了也能出名)
    return norm


def quote(code: str) -> dict:
    """单只实时行情(标准 quote dict)。"""
    return quotes([code]).get(_norm_code(code), {})


# ── 股票名称解析(独立于实时行情) ─────────────────────────────
# 中文名几乎是静态的。每次 quotes() 成功带名 → 落进持久 map(文件/Redis,TTL 默认30天)。
# 之后即便行情源临时不可达(如开盘抢数据时腾讯/东财抽风),综合选股 / 红蓝对抗 /
# 妙想第二意见 等仍能显示中文名,不再退化成 "600595 600595"(代码当名字)。
_NAME_TTL = int(_os.environ.get("DATAHUB_NAME_TTL", str(30 * 86400)))
_NAME_KEY = "names:_map"
_NAME_LOCK = _threading.Lock()


def _name_map() -> Dict[str, str]:
    m = _cache_get(_NAME_KEY, _NAME_TTL)
    return dict(m) if isinstance(m, dict) else {}


def _name_remember(quote_map: Dict[str, dict]) -> None:
    """从一批 quote dict 收割 code→name,合并进持久 map。纯数字名(=把代码当名)丢弃。"""
    if not isinstance(quote_map, dict):
        return
    fresh: Dict[str, str] = {}
    for code, q in quote_map.items():
        if not isinstance(q, dict):
            continue
        nm = str(q.get("name") or "").strip()
        c = _norm_code(code)
        if nm and c and not nm.isdigit():
            fresh[c] = nm
    if not fresh:
        return
    with _NAME_LOCK:
        m = _name_map()
        if any(m.get(k) != v for k, v in fresh.items()):   # 有变化才落盘
            m.update(fresh)
            _cache_put(_NAME_KEY, m, _NAME_TTL)


def stock_names(codes: List[str]) -> Dict[str, str]:
    """批量解析中文名 {6位code: 名称}。优先持久缓存(行情源挂了也有名),
    缺的才触发一次 quotes() 现拉(顺带把名字焐进缓存)。解析不到的不在返回里。"""
    norm = [_norm_code(c) for c in (codes or []) if c]
    if not norm:
        return {}
    m = _name_map()
    out = {c: m[c] for c in norm if c in m}
    missing = [c for c in norm if c not in out]
    if missing:
        try:
            q = quotes(missing)   # quotes() 内部会 _name_remember
            for c in missing:
                nm = str(q.get(c, {}).get("name") or "").strip()
                if nm and not nm.isdigit():
                    out[c] = nm
        except Exception:
            pass
    return out


def stock_name(code: str) -> str:
    """单只中文名;解析不到返回 ''(调用方自行决定是否回退成代码)。"""
    return stock_names([code]).get(_norm_code(code), "")


# ── K线磁盘缓存:日线日内不变,回测/因子/优化器反复拉同一批 → 缓存共享大幅提速 ──
# (_os 已在上方统一缓存层导入;此处自带专用缓存,不走通用 _dh_cache)
_KLINE_DIR = _os.path.join(_bootstrap.DB_DIR, "kline_cache")


def _kline_ttl() -> int:
    """K线磁盘缓存有效期(秒)。2026-06-26 拉长到 3 天:年K(日线序列)的历史 bar 永不变、
    只今日最新一根会动,而盘中最新价一律走 quotes(腾讯)补 + 盘后 prefetch 每晚刷新缓存 →
    3 天容差对均线/形态判断无实质影响,却大幅减少对 K线源(尤其 qfq 主源东财 push2his)的回源。
    配合 kline() 的"取数失败回退历史缓存 + 缓存不主动过期删除":源全挂时永远有历史 K线可用。
    env DATAHUB_KLINE_TTL_DAYS 可调(默认 3)。"""
    try:
        days = float(_os.environ.get('DATAHUB_KLINE_TTL_DAYS', '3'))
    except (TypeError, ValueError):
        days = 3.0
    return int(max(days, 0.01) * 86400)


def _sanitize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """K线数据质量护栏:丢弃违反 OHLC 不变量的脏行(会污染回测/因子/指标)。
    借鉴 Vibe-Trading 的 loader 边界校验:除收盘价 NaN/非正外,还查
      high>=low · high>=max(open,close) · low<=min(open,close) · 各价 >0,
    源断线重传偶发的错位/倒挂 bar(high<low、close 漂 0 等)一并清除。
    只清明确无效的行,不动复权跳变等正常波动;列名兼容大小写。任何异常原样返回。"""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        def _col(*names):
            for n in names:
                if n in df.columns:
                    return pd.to_numeric(df[n], errors="coerce")
            return None
        c = _col("close", "Close", "收盘")
        if c is None:
            return df
        good = c.notna() & (c > 0)
        o = _col("open", "Open", "开盘")
        h = _col("high", "High", "最高")
        lo = _col("low", "Low", "最低")
        for col in (o, h, lo):                      # 在场的 OHL 价也须 >0
            if col is not None:
                good &= col.notna() & (col > 0)
        if h is not None and lo is not None:
            good &= h >= lo                          # 最高 ≥ 最低
        if h is not None:                            # 最高须 ≥ 开/收
            if o is not None:
                good &= h >= o
            good &= h >= c
        if lo is not None:                           # 最低须 ≤ 开/收
            if o is not None:
                good &= lo <= o
            good &= lo <= c
        return df[good] if not good.all() else df
    except Exception:
        return df


# ── 东财历史K线原生源(2026-06-24 实测 push2his.eastmoney.com ~0.09s, 干净 JSON)──
# 作为 datahub 级 kline 第一兜底, 不依赖 StockDataFetcher 单体。输出格式严格对齐 fetcher:
# DatetimeIndex='Date' + 大写列 Open/Close/High/Low/Volume(177 处调用方依赖此格式)。
def _em_secid(code: str) -> str:
    """6 位代码 → 东财 secid('1.xxxxxx'=沪 / '0.xxxxxx'=深/京)。
    沪(1):6 开头股票(60 主板/68 科创)、5 开头基金(50-58 ETF/LOF)、11/13 开头债、900 B股。
    深/京(0):00/30(深股)、15/16/12(深基/债)、920/92/8x(北交所)、其余。
    ⚠️ '9' 单独有歧义:900xxx 是沪 B 股(→1), 920xxx 是北交所(→0), 故只把 '900' 归沪。"""
    c = _norm_code(code)
    if c[:1] in ('6', '5') or c[:2] in ('11', '13') or c[:3] == '900':
        return f'1.{c}'
    return f'0.{c}'


# 已知指数代码(与某些个股 6 位代码重码:000001=上证综指 vs 平安银行)。
# east 走个股 secid 会静默返回"重码个股"的 K线 → 当指数基准用就拿到错票。
# 对这些代码 east 直接放弃(返回空), 交回 fetcher / portfolio_backtest 的 akshare 指数路径。
_EM_INDEX_CODES = frozenset({
    '000001', '000010', '000016', '000300', '000688', '000852', '000903',
    '000905', '000906', '000688',
    '399001', '399005', '399006', '399300', '399905', '399852',
})


def _kline_eastmoney(code: str, period: str = "1y", interval: str = "1d",
                     adjust: str = "raw") -> pd.DataFrame:
    """东财 push2his 日线。adjust='raw'→fqt=0(不复权,与 fetcher 主源新浪同口径)/
    'qfq'→fqt=1(前复权,供技术分析两套缓存的 qfq 源)。
    返回 fetcher 同款格式(DatetimeIndex='Date' + 大写 OCHLV)或空 DF。仅日线。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()                  # 非日线交回主 fetcher 处理
    c = _norm_code(code)
    if c in _EM_INDEX_CODES:
        return pd.DataFrame()                  # 指数代码与个股重码, 放弃避免取到错票
    # ⭐ 限流(2026-06-24 审查修复: 原先漏导致 NameError 被静默吞, east 源 100% 失效)
    try:
        from rate_limiter import throttle as _throttle
    except Exception:
        def _throttle(*a, **k):
            return 0.0
    import urllib.request as _ur
    import json as _json
    lmt = int(_period_days(period) * 0.72) + 30   # 自然日→交易日约 ×0.72, 多取 30 根冗余
    secid = _em_secid(c)
    # fqt=0 不复权(raw,与新浪主源 scale=240&ma=no 同口径,2025-06-13 茅台两源一致);
    # fqt=1 前复权(qfq,技术分析两套缓存的 qfq 源)。raw 缓存须 fqt=0,否则历史价跳变污染。
    _fqt = '1' if adjust == 'qfq' else '0'
    url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?'
           f'secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57'
           f'&klt=101&fqt={_fqt}&end=20500101&lmt={lmt}')
    try:
        _throttle('eastmoney')
        req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = _ur.urlopen(req, timeout=6).read().decode('utf-8')   # 6s 短超时, 死源快失败
        klines = ((_json.loads(raw).get('data') or {}).get('klines')) or []
    except Exception:
        return pd.DataFrame()
    if not klines:
        return pd.DataFrame()
    rows = []
    for line in klines:
        p = line.split(',')             # date,open,close,high,low,volume(手),amount
        if len(p) < 6:
            continue
        try:
            # ⚠️ 东财成交量单位是"手"(100股), 而 fetcher 主源(新浪)是"股"。
            # 实测同票同日: 新浪 4480330股 = 东财 44803手 ×100。这里 ×100 对齐"股",
            # 否则量比/成交量均线/放量判断全缩 100 倍。
            rows.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                         float(p[5]) * 100))
        except (ValueError, IndexError):
            continue
    # 解析完整性护栏: 成功解析行数 < 收到行数的 80% → 视为响应残缺/损坏, 放弃交回 fetcher,
    # 避免 east 残缺数据通过 _route 的 non-empty 判定挤掉 fetcher 更完整的序列。
    if not rows or len(rows) < len(klines) * 0.8:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=['Date', 'Open', 'Close', 'High', 'Low', 'Volume'])
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    df = df.dropna(subset=['Date']).set_index('Date').sort_index()
    return df


def _kline_mootdx(code: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """mootdx 通达信公网日线(raw)——真·独立协议源:东财/新浪等 HTTP 源全被机房墙时的最后兜底
    (走通达信二进制协议、非 HTTP)。返回 fetcher 同款格式(DatetimeIndex='Date' + 大写 OCHLV,
    Volume 单位"股")或空 DF。仅日线。
    ⚠️ 需 `pip install mootdx`(httpx pin 与 mcp 冲突,已移出主依赖);未装/连不上 → 返回空 DF
       让 _route 跳过,无害。
    ⚠️ 成交量单位自适应:通达信日线 volume 多为"手",但为防版本差异污染**三方共享的 K线缓存**,
       用 amount/volume/close 反推均价倍率(≈100=手→×100, ≈1=股→×1),不靠外部源对比。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    try:
        from tdx_mootdx import get_kline as _tdx_k
        df = _tdx_k(_norm_code(code), frequency='day', count=800, adjust='')
    except Exception:
        return pd.DataFrame()
    if df is None or len(df) == 0 or not {'date', 'open', 'high', 'low', 'close', 'volume'}.issubset(df.columns):
        return pd.DataFrame()
    vol_mult = 100.0   # 默认"手"→"股"
    if 'amount' in df.columns:
        try:
            v = pd.to_numeric(df['volume'], errors='coerce')
            a = pd.to_numeric(df['amount'], errors='coerce')
            c = pd.to_numeric(df['close'], errors='coerce')
            m = (v > 0) & (a > 0) & (c > 0)
            if int(m.sum()) >= 5:
                ratio = float((a[m] / v[m] / c[m]).median())   # 手≈100, 股≈1
                vol_mult = 1.0 if abs(ratio - 1) < abs(ratio - 100) else 100.0
        except Exception:
            vol_mult = 100.0
    try:
        out = pd.DataFrame({
            # ⚠️ 通达信日线 date 带收盘时间 15:00:00,必须 normalize 到纯日期(00:00:00)对齐
            # 新浪/东财,否则 DatetimeIndex 对不上 → 污染三方共享 K线缓存、回测按日期取值错位
            'Date': pd.to_datetime(df['date'], errors='coerce').dt.normalize(),
            'Open': pd.to_numeric(df['open'], errors='coerce'),
            'Close': pd.to_numeric(df['close'], errors='coerce'),
            'High': pd.to_numeric(df['high'], errors='coerce'),
            'Low': pd.to_numeric(df['low'], errors='coerce'),
            'Volume': pd.to_numeric(df['volume'], errors='coerce') * vol_mult,
        }).dropna(subset=['Date']).set_index('Date').sort_index()
    except Exception:
        return pd.DataFrame()
    return _slice_by_days(out, _period_days(period))


def _sina_symbol(code: str) -> str:
    """6位代码 → 新浪带交易所前缀(sh600519/sz000001/bj830799)。qfq 主用个股(6/0/3)。"""
    c = _norm_code(code)
    if c[:3] in ('920',) or c[0] in ('4', '8'):
        return 'bj' + c
    if c[0] in ('0', '2', '3'):
        return 'sz' + c
    return 'sh' + c   # 6/9/5 开头(沪)及兜底


def _kline_baostock(code: str, period: str = "1y", interval: str = "1d",
                    adjust: str = "raw") -> pd.DataFrame:
    """baostock(证券宝)日线 —— 免费全历史(1990至今)+ 独立兜底源(非腾讯/新浪/东财)。
    封装在 data/baostock_safe.py(惰性登录 + 全局锁串行 + 未装返空)。raw=不复权 / qfq=前复权。"""
    try:
        import baostock_safe as _bao
        return _bao.kline(code, period, interval, adjust)
    except Exception:
        return pd.DataFrame()


def _kline_sina_qfq(code: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """新浪前复权(qfq)日线 —— **唯一真非东财** qfq 源(akshare stock_zh_a_daily, finance.sina.com.cn)。

    原 qfq 链 east_qfq(东财 push2his fqt=1)+ akshare_qfq(stock_zh_a_hist 也走东财)**全是东财**,
    机房 IP 被封时整个 qfq 取不到 → 技术分析只能退 raw(除权跳空毁形态/缠论/因子)。新浪 stock_zh_a_daily
    用复权因子本地算 qfq, 与东财同口径(实测:最新价对齐 raw、历史价下移), 是 qfq 的真跨公司兜底。
    返回 fetcher 同款格式(DatetimeIndex='Date' + 大写 OCHLV, Volume 单位'股')或空 DF。仅日线。
    ⚠️ 该接口 volume 本就是'股'(非 akshare '手'), **不乘 100**;返回全历史, 由 _slice_by_days 截到 period。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    need = ('date', 'open', 'close', 'high', 'low', 'volume')
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
        df = ak_call(ak.stock_zh_a_daily, symbol=_sina_symbol(code), adjust='qfq', timeout=20)
    except Exception:
        return pd.DataFrame()
    if df is None or getattr(df, 'empty', True) or not all(c in df.columns for c in need):
        return pd.DataFrame()
    try:
        out = pd.DataFrame({
            'Date': pd.to_datetime(df['date'], errors='coerce').dt.normalize(),
            'Open': pd.to_numeric(df['open'], errors='coerce'),
            'Close': pd.to_numeric(df['close'], errors='coerce'),
            'High': pd.to_numeric(df['high'], errors='coerce'),
            'Low': pd.to_numeric(df['low'], errors='coerce'),
            'Volume': pd.to_numeric(df['volume'], errors='coerce'),  # 已是'股', 不乘 100
        }).dropna(subset=['Date']).set_index('Date').sort_index()
    except Exception:
        return pd.DataFrame()
    return _slice_by_days(out, _period_days(period))


_TF_CLIENT = None


def _tickflow_client():
    """TickFlow 免费客户端(模块级懒加载单例)。抑制其初始化 banner 噪音。"""
    global _TF_CLIENT
    if _TF_CLIENT is None:
        import io as _io
        import contextlib as _cl
        from tickflow import TickFlow
        with _cl.redirect_stdout(_io.StringIO()):
            _TF_CLIENT = TickFlow.free()
    return _TF_CLIENT


def _tickflow_symbol(code: str) -> str:
    """6位代码 → TickFlow 格式(600519.SH / 000001.SZ / 830799.BJ)。"""
    c = _norm_code(code)
    if c[:3] in ('920',) or c[0] in ('4', '8'):
        return c + '.BJ'
    if c[0] in ('0', '2', '3'):
        return c + '.SZ'
    return c + '.SH'


def _kline_tickflow_qfq(code: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """TickFlow 免费日K前复权 —— qfq **末位**真非东财兜底(`free-api.tickflow.org`,独立第三方)。

    实测前复权(最新价对齐 raw、历史价下移),与东财/新浪 qfq 同口径。⚠️ 限流严(免费版 10次/分、
    1标的/次、盘中不实时)→ `throttle('tickflow')` 6s,且**只作 east_qfq/akshare_qfq/sina_qfq 全挂时的
    现调兜底,不进盘后预热**(预热用 sina 足够)。返回 fetcher 同款格式(DatetimeIndex='Date' + 大写
    OCHLV,Volume 单位'股')或空 DF。仅日线。⚠️ TickFlow volume 单位'手' → ×100 对齐'股'。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    need = ('trade_date', 'open', 'high', 'low', 'close', 'volume')
    try:
        from rate_limiter import throttle
        throttle('tickflow')
        cnt = max(int(_period_days(period) * 0.7) + 15, 30)
        df = _tickflow_client().klines.get(_tickflow_symbol(code), period='1d', count=cnt, as_dataframe=True)
    except Exception:
        return pd.DataFrame()
    if df is None or getattr(df, 'empty', True) or not all(c in df.columns for c in need):
        return pd.DataFrame()
    try:
        out = pd.DataFrame({
            'Date': pd.to_datetime(df['trade_date'], errors='coerce').dt.normalize(),
            'Open': pd.to_numeric(df['open'], errors='coerce'),
            'Close': pd.to_numeric(df['close'], errors='coerce'),
            'High': pd.to_numeric(df['high'], errors='coerce'),
            'Low': pd.to_numeric(df['low'], errors='coerce'),
            'Volume': pd.to_numeric(df['volume'], errors='coerce') * 100,  # 手→股
        }).dropna(subset=['Date']).set_index('Date').sort_index()
    except Exception:
        return pd.DataFrame()
    return _slice_by_days(out, _period_days(period))


def kline(code: str, period: str = "1y", interval: str = "1d", use_cache: bool = True,
          adjust: str = "raw") -> pd.DataFrame:
    """K线 DataFrame(DatetimeIndex='Date', 列 Open/Close/High/Low/Volume)。
    ⭐ 两套复权缓存(2026-06-24):
      adjust='raw'(默认):不复权,真实成交价 —— 回测/持仓盈亏/决策后验/显示真实价 用。
        源链:StockDataFetcher(新浪 raw 主源)→ 东财 push2his fqt=0 → mootdx 通达信。缓存键无后缀。
      adjust='qfq':前复权,消除除权跳空 —— InStock/形态/缠论/因子/技术指标 用(行业标准)。
        源链:东财 push2his fqt=1 → akshare qfq(均走东财,东财封时取不到 → fallback raw,**不写 qfq 缓存**)。
        新浪只能 raw 不入 qfq 源;mootdx 只 raw 不入 qfq 源。缓存键 _qfq 后缀,与 raw 互不污染。
    健康度路由自动把可达源排前。磁盘缓存日线提速。失败返回空 DF。
    use_cache=False 强制实时拉(需要今日最新 bar 时用)。"""
    adjust = 'qfq' if str(adjust) == 'qfq' else 'raw'
    suffix = '_qfq' if adjust == 'qfq' else ''
    cache_f = _os.path.join(_KLINE_DIR, f"{_norm_code(code)}_{period}_{interval}{suffix}.pkl")
    if use_cache:
        try:
            if _os.path.isfile(cache_f) and (_time.time() - _os.path.getmtime(cache_f)) < _kline_ttl():
                df = pd.read_pickle(cache_f)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df
        except Exception:
            pass

    # baostock(证券宝)免费全历史(1990至今)+ 稳定(不像东财常被反爬封):
    #   · 长周期(≥2y)居首拿全历史(解新浪~365根回测深度限制);
    #   · 短周期排**新浪之后、东财之前** —— 新浪(实测可达+支持并发+有当日bar)当主源,baostock 作首选兜底
    #     **优先于常被封的东财**(用户诉求:东财老被限,稳定免费源能用就先用)。
    #   ⚠️ baostock 不放第一:它(a)不可并发(全局锁串行,批量会卡)(b)当日K线 17:30 才入库(16:30 预热缺今日)。
    _long_hist = period in ('2y', '3y', '5y')
    # 长历史(≥2y)**直调 baostock**(绕过健康路由):只有 baostock 能给全量(新浪/东财日K均~365根上限),
    # 而 _route 按健康度排序会让健康的新浪抢先返回 365 根 → 静态把 baostock 放第一也没用。故长历史直接
    # 优先调 baostock,非空即用、写缓存返回;未装/失败/空 再落下面常规多源链(只能~365,有数据胜无)。
    if _long_hist:
        _bl = _sanitize_kline(_kline_baostock(code, period, interval, adjust))
        if isinstance(_bl, pd.DataFrame) and not _bl.empty:
            if use_cache:
                try:
                    _os.makedirs(_KLINE_DIR, exist_ok=True)
                    _bl.to_pickle(cache_f)
                except Exception:
                    pass
            return _bl

    if adjust == 'raw':
        def _f():
            df = _fetcher().get_stock_data(code, period, interval)   # StockDataFetcher 默认 raw
            return pd.DataFrame() if isinstance(df, dict) else (df if isinstance(df, pd.DataFrame) else pd.DataFrame())
        # 新浪主源(可达+并发+有当日bar)→ baostock(免费稳定,**优先于常被封的东财**)→ 东财 → mootdx
        df = _route("kline",
                    [("fetcher", _f),
                     ("baostock", lambda: _kline_baostock(code, period, interval, 'raw')),
                     ("east", lambda: _kline_eastmoney(code, period, interval, 'raw')),
                     ("mootdx", lambda: _kline_mootdx(code, period, interval))],
                    empty=pd.DataFrame(), timeout=45)
    else:  # qfq:east 走东财,sina_qfq/baostock_qfq 真非东财兜底
        # ⚡ 健康度短路(2026-06-25 修):qfq 源在**活跃冷却期**(连续失败,冷却额外 -1.0 → score<-0.5)
        # 时每只吃满源超时(原 45s×2=90s)会拖垮 unified_selection 等批量任务 → 直接走 raw,0 成本降级。
        # ⚠️ 必须**全部 qfq 源都冷却**才退 raw:east_qfq 是东财,sina_qfq/baostock_qfq 是真非东财——
        #    东财被封时 east 冷却但 sina/baostock 仍健康(0.5> -0.5)→ 不短路 → _route 走它们拿真 qfq。
        #    漏掉真非东财源会让"东财一封就退 raw"白白浪费可达源(2026-06-25 补 sina 源时同步修)。
        # ⚠️ 阈值用 -0.5 不用 0:冷却期(120s内)score≈-1.2 才短路;冷却过期后 score≈-0.2(不短路)
        # → 每 120s 自动重试,网络/东财恢复即自愈(用 0 会因 rate=0 永久短路、死锁不恢复)。
        # ⚠️ 2026-06-27 阶段1重构:删 akshare_qfq(ak.stock_zh_a_hist 实走东财 push2his,是 east_qfq 的
        #    二道贩子冗余,东财封时与 east 同死、不封时被 east 覆盖,纯多打一次东财)。短路判断同步去掉它。
        _now = _time.time()
        if (_health('kline_qfq:east_qfq', _now) < -0.5
                and _health('kline_qfq:sina_qfq', _now) < -0.5
                and _health('kline_qfq:tickflow_qfq', _now) < -0.5
                and _health('kline_qfq:baostock_qfq', _now) < -0.5):
            return kline(code, period, interval, use_cache=use_cache, adjust='raw')
        # 短超时 10s。**优先真非东财源**(用户诉求:东财常被封):sina_qfq(新浪,快)→ baostock_qfq(稳,全历史)
        # → east_qfq(东财,降级)→ tickflow_qfq(限流慢,末位)。东财封时前两个非东财源顶上,
        # 技术分析仍有真 qfq、不必退 raw。长周期(≥2y)baostock_qfq 居首拿全历史。
        _sina_q = ("sina_qfq", lambda: _kline_sina_qfq(code, period, interval))
        _bao_q = ("baostock_qfq", lambda: _kline_baostock(code, period, interval, 'qfq'))
        _east_q = ("east_qfq", lambda: _kline_eastmoney(code, period, interval, 'qfq'))
        _tick_q = ("tickflow_qfq", lambda: _kline_tickflow_qfq(code, period, interval))
        # 长历史(≥2y)已在上面直调 baostock,这里只管常规多源(短周期/长历史 baostock 失败的兜底)
        df = _route("kline_qfq", [_sina_q, _bao_q, _east_q, _tick_q],
                    empty=pd.DataFrame(), timeout=10)
    df = _sanitize_kline(df)
    # 取数成功 → 写缓存(刷新 mtime)返回
    if isinstance(df, pd.DataFrame) and not df.empty:
        if use_cache:
            try:
                _os.makedirs(_KLINE_DIR, exist_ok=True)
                df.to_pickle(cache_f)
            except Exception:
                pass
        return df
    # —— 取数失败(空 DF)——
    # ① 历史缓存兜底(即使已过 TTL):缓存不主动过期删除,源全挂时永远有历史 K线可用(2026-06-26)。
    #    回测/因子/技术分析宁可用几天前的历史序列,也好过空 DF 直接断流。
    if use_cache:
        try:
            if _os.path.isfile(cache_f):
                stale = pd.read_pickle(cache_f)
                if isinstance(stale, pd.DataFrame) and not stale.empty:
                    _age_d = int((_time.time() - _os.path.getmtime(cache_f)) / 86400)
                    print(f'[datahub.kline] {_norm_code(code)} {period}{suffix} 取数失败,'
                          f'回退历史缓存(age {_age_d}d)', flush=True)
                    return stale
        except Exception:
            pass
    # ② qfq 且无历史缓存可兜 → 退回 raw(技术分析有数据胜过无;raw 自身也走①历史兜底)。绝不写 qfq 缓存防污染。
    if adjust == 'qfq':
        return kline(code, period, interval, use_cache=use_cache, adjust='raw')
    return df   # raw 也失败且无历史缓存 → 空 DF


# ── K线缓存预热(每日盘后焐热目标股票池,供回测/因子/晨报/持仓守卫命中暖缓存)──
# 主源(新浪日K / 东财 push2his)均返回**不复权(raw)**完整日线序列(2026-06-24 实测两源
# 同票同日 raw 价一致, 见 _kline_eastmoney 注释)。全项目 K线统一 raw 口径。
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


def _write_kline_cache(code: str, period: str, interval: str, df: pd.DataFrame,
                       adjust: str = "raw") -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False
    try:
        _os.makedirs(_KLINE_DIR, exist_ok=True)
        suffix = '_qfq' if adjust == 'qfq' else ''
        df.to_pickle(_os.path.join(_KLINE_DIR, f"{_norm_code(code)}_{period}_{interval}{suffix}.pkl"))
        return True
    except Exception:
        return False


def prefetch_kline(code: str, periods=("1y", "6mo", "1mo"), interval: str = "1d",
                   adjust: str = "both") -> dict:
    """预拉一只股票 K线写各 period 磁盘缓存。主拉最长 period 一次,短 period 切片派生(零额外请求)。
    ⭐ adjust='both'(默认):同时焐 raw + qfq 两套缓存,供技术分析(qfq)和回测/持仓(raw)盘后命中暖缓存,
    避免盘中逐只冷拉 qfq(akshare 慢/东财封 → 吃源超时拖垮 unified_selection 等任务)。
      - raw:新浪主源(快,全量)
      - qfq:**仅东财 push2his fqt=1**(快速源,6s 失败即跳过不写防污染;不挂 akshare 慢源以免拖垮预热;
        东财封时焐不上 → 盘中 kline(qfq) 自会 fallback raw,有数据不崩)
    返回 {code, bars, wrote}。失败 bars=0。"""
    base = max(periods, key=_period_days) if periods else "1y"
    out = {"code": code, "bars": 0, "wrote": 0}

    # ── raw(新浪全量)──
    if adjust in ('raw', 'both'):
        def _f():
            d = _fetcher().get_stock_data(code, base, interval)
            return pd.DataFrame() if isinstance(d, dict) else (d if isinstance(d, pd.DataFrame) else pd.DataFrame())
        df = _route("kline", [("fetcher", _f)], empty=pd.DataFrame(), timeout=45)
        if isinstance(df, pd.DataFrame) and not df.empty:
            out["bars"] = int(len(df))
            for p in periods:
                sub = df if p == base else _slice_by_days(df, _period_days(p))
                if _write_kline_cache(code, p, interval, sub, adjust='raw'):
                    out["wrote"] += 1

    # ── qfq(仅东财快速源,焐技术分析两套缓存的 qfq 侧)──
    if adjust in ('qfq', 'both'):
        try:
            qdf = _route("kline_qfq_prefetch",
                         [("east_qfq", lambda: _kline_eastmoney(code, base, interval, 'qfq')),
                          ("sina_qfq", lambda: _kline_sina_qfq(code, base, interval))],
                         empty=pd.DataFrame(), timeout=10)
            qdf = _sanitize_kline(qdf)
            if isinstance(qdf, pd.DataFrame) and not qdf.empty:
                for p in periods:
                    sub = qdf if p == base else _slice_by_days(qdf, _period_days(p))
                    if _write_kline_cache(code, p, interval, sub, adjust='qfq'):
                        out["wrote"] += 1
        except Exception:
            pass
    return out


def kline_akshare_compat(code: str, period: str = "1y") -> pd.DataFrame:
    """走 kline()(磁盘缓存)取数据, 返回 akshare ak.stock_zh_a_hist 风格的
    中文列(日期/开盘/收盘/最高/最低/成交量, 0-based index)。

    用途:让 monitor / 老分析代码 无缝替换 `ak.stock_zh_a_hist(...)` 调用,
    同时自动获得 datahub 的磁盘缓存。返回空 DataFrame 表示获取失败。
    """
    df = kline(code, period)
    if df is None or df.empty:
        return pd.DataFrame()
    # datahub 内部统一 Date index + 大写英文列;转回 akshare 兼容格式
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index()
    rename = {
        'Date': '日期', 'date': '日期',
        'Open': '开盘', 'open': '开盘',
        'Close': '收盘', 'close': '收盘',
        'High': '最高', 'high': '最高',
        'Low': '最低', 'low': '最低',
        'Volume': '成交量', 'volume': '成交量',
    }
    return df.rename(columns={k: v for k, v in rename.items() if k in df.columns})


def kline_with_indicators(code: str, period: str = "1y") -> pd.DataFrame:
    """K线 + MyTT 技术指标(MA/MACD/KDJ/RSI/BOLL...)。失败返回空 DF。
    ⭐ 技术指标用前复权 qfq(除权跳空会让均线/MACD 失真);qfq 取不到时内部 fallback raw。"""
    f = _fetcher()
    df = kline(code, period, adjust='qfq')   # qfq:技术指标行业标准
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

def _north_flow_akshare(days: int = 30) -> List[dict]:
    """北向 akshare 兜底源,产出与主源逐字段同构
    {trade_date/hgt_yi/sgt_yi/net_total(亿元)/source/net_hgt/net_sgt(元)/net_tgt}。
    ⚠️ 北向 2024-08 起官方停实时披露,此源大概率已失效;列对不齐/空/异常/非日序列 → 返回 [] 让 _route 退回主源,无害。"""
    need = ['日期', '北向资金-成交净买额', '沪股通-成交净买额', '深股通-成交净买额']
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
        df = ak_call(ak.stock_hsgt_fund_flow_summary_em, timeout=20)
    except Exception:
        return []
    if df is None or getattr(df, 'empty', True) or not all(c in df.columns for c in need):
        return []

    def _yi(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    out = []
    for _, r in df.head(days).iterrows():
        hgt, sgt = _yi(r['沪股通-成交净买额']), _yi(r['深股通-成交净买额'])
        out.append({'trade_date': str(r['日期']), 'hgt_yi': hgt, 'sgt_yi': sgt,
                    'net_total': _yi(r['北向资金-成交净买额']), 'source': 'akshare',
                    'net_hgt': hgt * 1e8, 'net_sgt': sgt * 1e8, 'net_tgt': 0})
    # 防误用:若不是"按日"序列(日期全同 = 当日各通道汇总表),弃用,避免重复同日污染
    if len(out) > 1 and len({r['trade_date'] for r in out}) <= 1:
        return []
    return out


def north_flow(days: int = 30) -> List[dict]:
    """北向资金近 N 日。list[dict] 键:trade_date/hgt_yi/sgt_yi/net_total(亿元)+ net_hgt/net_sgt(元)/net_tgt。
    源:北向本地缓存(同花顺,主;2026-06-27 阶段1:dsm 内 adata 兜底已删)→ akshare 沪深港通汇总(兜底,实质失效)。
    ⚠️ 2026-06-24 实测:akshare 走的东财 datacenter 接口活着但 FUND_INFLOW=null(北向 2024-08 官方停实时
    披露已坐实),拿不到有效净流入 → 真数据只能靠主源同花顺本地缓存(jobs 每日 15:40 入库)。akshare 留作
    占位,值无效时被 _north_flow_akshare 的防御拦截、_route 退回主源,无害。"""
    from data_source_manager import data_source_manager as dsm
    return _route("north_flow",
                  [("dsm", lambda: dsm.get_north_flow_a_data(days)),
                   ("akshare", lambda: _north_flow_akshare(days))],
                  empty=[]) or []


def _capital_flow_akshare(code: str, days: int = 120) -> List[dict]:
    """个股资金流 akshare 兜底源,产出与主源逐字段同构 {date/main_net/small_net/mid_net/large_net/super_net}(元)。
    列对不齐/空/异常 → 返回 [] 让 _route 跳过(绝不污染下游)。"""
    cmap = {'日期': 'date', '主力净流入-净额': 'main_net', '小单净流入-净额': 'small_net',
            '中单净流入-净额': 'mid_net', '大单净流入-净额': 'large_net', '超大单净流入-净额': 'super_net'}
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
        mk = 'sh' if str(code).startswith('6') else 'sz'   # 北交所 akshare 不支持,主源已覆盖
        df = ak_call(ak.stock_individual_fund_flow, stock=str(code), market=mk, timeout=20)
    except Exception:
        return []
    if df is None or getattr(df, 'empty', True) or not all(c in df.columns for c in cmap):
        return []
    out = []
    for _, r in df.tail(days).iterrows():
        row = {'date': str(r['日期'])}
        for zh, en in cmap.items():
            if en == 'date':
                continue
            try:
                row[en] = float(r[zh])
            except (ValueError, TypeError):
                row[en] = 0.0
        out.append(row)
    return out


def capital_flow(code: str, days: int = 120) -> List[dict]:
    """个股历史资金流(日级)。list[dict] 键:date/main_net/small_net/mid_net/large_net/super_net(元)。
    源:东财 push2his(主)→ akshare(弱兜底)。⚠️ 2026-06-24 实测:akshare stock_individual_fund_flow
    底层与主源**同走东财 push2his fflow 端点**,非真跨源——东财 IP 级被封时主备同死,仅防东财子接口
    偶发抽风+akshare 换解析路径恰好成功的弱场景。个股历史资金流东财近乎垄断,暂无真跨公司替代源。"""
    return _route("capital_flow",
                  [("a_stock", lambda: _adapter().get_fund_flow_history(code, days)),
                   ("akshare", lambda: _capital_flow_akshare(code, days))],
                  empty=[]) or []


def capital_flow_minute(code: str) -> List[dict]:
    """个股当日分钟级资金流。list[dict]。"""
    return _route("capital_flow_minute", [("a_stock", lambda: _adapter().get_fund_flow_minute(code))], empty=[]) or []


def capital_flow_adata(code: str) -> List[dict]:
    """个股历史日度资金流(**兼容别名**)。2026-06-27 阶段1:adata(二道贩子)已删 →
    本域改返回 canonical `capital_flow(code)`(东财 push2his,真实各档净流入 date/main_net/small_net/
    mid_net/large_net/super_net,元)。保留函数名供旧调用方(agents/ai_workflow/MCP/API)零改签名;
    数据从 adata 占位升级为东财真值。各档不同构污染口径的顾虑随 adata 删除一并消解。"""
    return capital_flow(code)


def _dragon_tiger_eastmoney(trade_date: str = None, page_size: int = 400) -> List[dict]:
    """全市场龙虎榜明细 —— 东财数据中心直连(RPT_DAILYBILLBOARD_DETAILSNEW),替代原 adata 源(已删)。
    trade_date='YYYY-MM-DD' 指定日;None → 取最近一个有龙虎榜数据的交易日(按 TRADE_DATE 降序取首日,
    自然处理"盘中今日龙虎榜未出 → 退到上一交易日")。归一含 stock_code/stock_name(供 wf_daily_strategy_scan
    池子加载 r.get('stock_code'))+ 净买/买卖额/原因。任何异常返 [](交 _route 跳过/上层无害)。"""
    from a_stock_data_adapter import _eastmoney_datacenter
    flt = f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')" if trade_date else ""
    rows = _eastmoney_datacenter('RPT_DAILYBILLBOARD_DETAILSNEW', filter_str=flt,
                                 page_size=page_size, sort_columns='TRADE_DATE', sort_types='-1')
    if not rows:
        return []
    if not trade_date:   # 只保留最近一个交易日的行(东财按 TRADE_DATE 降序返回 → 首行即最近日)
        latest = str(rows[0].get('TRADE_DATE', ''))[:10]
        rows = [r for r in rows if str(r.get('TRADE_DATE', ''))[:10] == latest]

    def _num(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    out = []
    for r in rows:
        out.append({
            'trade_date': str(r.get('TRADE_DATE', ''))[:10],
            'stock_code': str(r.get('SECURITY_CODE', '') or ''),
            'stock_name': r.get('SECURITY_NAME_ABBR', '') or '',
            'reason': r.get('EXPLANATION', '') or '',
            'net_buy': round(_num(r.get('BILLBOARD_NET_AMT')) / 10000, 1),     # 万元
            'buy_amt': round(_num(r.get('BILLBOARD_BUY_AMT')) / 10000, 1),     # 万元
            'sell_amt': round(_num(r.get('BILLBOARD_SELL_AMT')) / 10000, 1),   # 万元
            'change_pct': round(_num(r.get('CHANGE_RATE')), 2),
            'turnover': round(_num(r.get('TURNOVERRATE')), 2),
        })
    out.sort(key=lambda x: x['net_buy'], reverse=True)   # 净买额降序
    return out


def dragon_tiger(trade_date: str = None) -> List[dict]:
    """全市场龙虎榜明细(指定/最近交易日)。list[dict],键:trade_date/stock_code/stock_name/reason/
    net_buy/buy_amt/sell_amt(万元)/change_pct/turnover。
    源:东财数据中心直连(2026-06-27 阶段1:原 adata 源 list_a_list_daily 已删,口径迁东财 RPT)。"""
    return _route("dragon_tiger", [("em_datacenter", lambda: _dragon_tiger_eastmoney(trade_date))], empty=[]) or []


def dragon_tiger_stock(code: str, trade_date: str = None, look_back: int = 30) -> dict:
    """个股龙虎榜上榜记录。dict。"""
    return _route("dragon_tiger_stock",
                  [("a_stock", lambda: _adapter().get_dragon_tiger(code, trade_date, look_back))], empty={}) or {}


def dragon_tiger_detail(trade_date: str = None, page_size: int = 200) -> List[dict]:
    """全市场龙虎榜明细(东财数据中心 RPT_DAILYBILLBOARD_DETAILSNEW),按净买入降序。list[dict],
    键为东财 RPT 原字段(SECURITY_CODE/SECURITY_NAME_ABBR/BILLBOARD_NET_AMT/BILLBOARD_BUY_AMT/
    BILLBOARD_SELL_AMT/EXPLANATION/CHANGE_RATE…)。与 dragon_tiger(走 dsm,字段不同构)互补,
    此函数保 RPT 原字段口径供盘前龙虎榜报告/盘后综述用。"""
    if not trade_date:
        return []
    flt = f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')"

    def _q():
        from a_stock_data_adapter import _eastmoney_datacenter
        return _eastmoney_datacenter('RPT_DAILYBILLBOARD_DETAILSNEW', filter_str=flt,
                                     page_size=page_size, sort_columns='BILLBOARD_NET_AMT', sort_types='-1')
    return _route("dragon_tiger_detail", [("em_datacenter", _q)], empty=[]) or []


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
    """板块涨跌排名 {top,bottom,total}。sector_type: industry/concept。
    源:东财 push2(主)→ 同花顺 data.10jqka(**真跨公司**兜底,2026-06-25:东财机房 IP 被封时仍可取)。
    ⚠️ 排名是 dict,东财失败返回的空结构 {top:[],total:0} 仍 truthy、会被 _route 当"成功"而不 fallback,
    故 thunk 用 _rank_or_none 把空结构(total=0)显式转 None → _route 正确降级到同花顺。"""
    a = _adapter()
    fn = a.get_concept_ranking if sector_type == "concept" else a.get_industry_ranking
    def _rank_or_none(d):
        return d if isinstance(d, dict) and d.get("total") else None
    return _route(f"sector_ranking_{sector_type}",
                  [("em_push2", lambda: _rank_or_none(fn(top_n))),
                   ("ths", lambda: _rank_or_none(a.get_sector_ranking_ths(sector_type, top_n)))],
                  empty={"top": [], "bottom": [], "total": 0}) or {"top": [], "bottom": [], "total": 0}


def _sector_spot_sina() -> List[dict]:
    """板块快照新浪行业源,产出 [{板块,涨跌幅,领涨}](涨幅降序)。列对不齐/空/异常→[]。"""
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
        df = ak_call(ak.stock_sector_spot, indicator="新浪行业", timeout=20)
    except Exception:
        return []
    if df is None or getattr(df, 'empty', True) or '板块' not in df.columns or '涨跌幅' not in df.columns:
        return []
    rows = [{"板块": r.get("板块"), "涨跌幅": round(float(r.get("涨跌幅") or 0), 2),
             "领涨": r.get("股票名称") or ""} for _, r in df.iterrows()]
    return sorted(rows, key=lambda x: x["涨跌幅"], reverse=True)


def _sector_spot_ths() -> List[dict]:
    """板块快照同花顺行业源(真跨源:不走东财/新浪),产出与新浪源逐字段同构。列对不齐/空/异常→[]。"""
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
        df = ak_call(ak.stock_board_industry_summary_ths, timeout=20)
    except Exception:
        return []
    if df is None or getattr(df, 'empty', True) or '板块' not in df.columns or '涨跌幅' not in df.columns:
        return []
    rows = [{"板块": r.get("板块"), "涨跌幅": round(float(r.get("涨跌幅") or 0), 2),
             "领涨": r.get("领涨股") or ""} for _, r in df.iterrows()]
    return sorted(rows, key=lambda x: x["涨跌幅"], reverse=True)


def sector_spot(top_n: int = 8, bottom_n: int = 5) -> dict:
    """行业板块快照(涨跌排序)。返回 {top:[{板块,涨跌幅,领涨}], bottom:[...]}。
    并列双源(2026-06-24):新浪行业(主)→ 同花顺行业(真跨源兜底,不走东财/新浪)。
    注:两家行业分类体系不同(新浪~49个/同花顺~90个),兜底时板块名会变成对应家的分类,
    但用途是"看哪些行业强/弱",两套都成立;key 集一致,下游不受影响。"""
    rows = _route("sector_spot",
                  [("sina", _sector_spot_sina), ("ths", _sector_spot_ths)],
                  empty=[]) or []
    return {"top": rows[:top_n], "bottom": rows[-bottom_n:]} if rows else {"top": [], "bottom": []}


def sector_fund_flow(sector_type: str = "industry", top_n: int = 50) -> List[dict]:
    """板块资金流(行业/概念)。list[dict](金额单位=元)。
    主源东财 push2 → datacenter getbkzj(仍东财,push2 偶发抽风时换子接口)→ 同花顺 data.10jqka
    (**真跨公司**兜底,2026-06-25:前两者都是东财,IP 被封时同死,同花顺接上保住板块资金流不全空)。"""
    return _route(f"sector_fund_flow_{sector_type}",
                  [("push2", lambda: _adapter().get_sector_fund_flow(sector_type, top_n)),
                   ("bkzj", lambda: _adapter().get_sector_fund_flow_bkzj(sector_type, top_n)),
                   ("ths", lambda: _adapter().get_sector_fund_flow_ths(sector_type, top_n))],
                  empty=[]) or []


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


def stock_reports(code: str, max_pages: int = 3) -> List[dict]:
    """个股研报列表(东财 qType=0)。list[dict]。"""
    return _route("stock_reports",
                  [("a_stock", lambda: _adapter().get_reports(code, max_pages))], empty=[]) or []


def industry_reports(industry_code: str = "*", max_pages: int = 5,
                     begin: str = "2024-01-01") -> List[dict]:
    """东财行业研报列表(qType=1)。industry_code='*' 全行业,或具体行业代码。list[dict]。"""
    return _route("industry_reports",
                  [("a_stock", lambda: _adapter().get_industry_reports(industry_code, max_pages, begin))],
                  empty=[]) or []


# ══════════════════════════════════════════════════════════
#  新闻/公告域
# ══════════════════════════════════════════════════════════

def _stock_news_akshare(code: str, page_size: int = 20) -> List[dict]:
    """个股新闻 akshare 兜底源,产出与主源逐字段同构 {title/content/time/source/url}。
    列对不齐/空/异常 → 返回 [] 让 _route 跳过。"""
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
        df = ak_call(ak.stock_news_em, symbol=str(code), timeout=20)
    except Exception:
        return []
    if df is None or getattr(df, 'empty', True) or '新闻标题' not in df.columns:
        return []
    out = []
    for _, r in df.head(page_size).iterrows():
        out.append({'title': str(r.get('新闻标题', '')),
                    'content': str(r.get('新闻内容', ''))[:200],
                    'time': str(r.get('发布时间', '')),
                    'source': str(r.get('文章来源', '')),
                    'url': str(r.get('新闻链接', ''))})
    return out


def stock_news(code: str, page_size: int = 20) -> List[dict]:
    """个股新闻。list[dict] 键:title/content/time/source/url。
    源:东财搜索(dsm 主)→ akshare stock_news_em(弱兜底)。⚠️ 2026-06-24 实测:akshare stock_news_em
    底层与主源**同走东财 search-api-web 端点**,非真跨源。真跨源候选已实测排除:同花顺 news.10jqka
    返回的是泛财经快讯(stock=[],非个股新闻)、新浪需中文名传参——故暂以东财同源弱兜底为准。"""
    from data_source_manager import data_source_manager as dsm
    return _route("stock_news",
                  [("dsm", lambda: dsm.get_stock_news_a_stock(code, page_size)),
                   ("akshare", lambda: _stock_news_akshare(code, page_size))],
                  empty=[]) or []


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
    源链:财联社电报(dsm,主)→ 东财全球快讯(akshare,兜底)。2026-06-25:财联社是真跨源,提为主源,
    东财降兜底以减少东财调用(财经快讯对决策权重低,财联社口径完全够用)。"""
    return _route("market_news",
                  [("cls", lambda: _news_cls(page_size)), ("em", lambda: _news_em(page_size))], empty=[]) or []


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
              "stock_reports", "industry_reports",
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
    stock_reports = staticmethod(stock_reports)
    industry_reports = staticmethod(industry_reports)
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
