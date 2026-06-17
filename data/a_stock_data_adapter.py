"""
A 股全栈数据适配器
基于 a-stock-data (V3.1) 的直连 HTTP API，零 akshare/tushare 依赖

数据源: 腾讯财经(PE/PB/市值) + 东财push2(资金流/行业) + 东财datacenter(龙虎榜/解禁/融资融券/大宗交易/股东户数/分红)
       + 同花顺(强势股/题材/一致预期) + 百度股市通(概念板块) + 财联社(快讯) + 新浪(财报三表)
       + mootdx(K线/盘口) + 巨潮(公告)

使用方法:
    from a_stock_data_adapter import adapter
    info = adapter.get_stock_info("688017")
    flow = adapter.get_fund_flow("688017")
"""

import urllib.request
import json
import re
import math
import uuid
import os
import warnings
from datetime import datetime, timedelta
from typing import Optional
from io import StringIO

import requests
import pandas as pd
warnings.filterwarnings('ignore', category=FutureWarning, module='pandas')
warnings.filterwarnings('ignore', message='Passing literal html')

# ============================================================
# 全局配置
# ============================================================
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DEFAULT_TIMEOUT = 15

# 国内数据源（东财/腾讯/新浪等）不需要走代理
# 建一个 trust_env=False 的 session，彻底忽略 http_proxy/https_proxy 环境变量
# 所有 a-stock 内部请求都走这个 session（替换原 requests.get/post）
_session = requests.Session()
_session.trust_env = False

# 自限流:包装 _session,所有 .get 按 host(东财/腾讯/新浪)自动节流,降低封禁风险
try:
    from rate_limiter import throttled_session, throttle as _throttle
    throttled_session(_session)
except Exception:
    def _throttle(*a, **k):
        return 0.0


# ============================================================
# 工具函数
# ============================================================

def _get_prefix(code: str) -> str:
    """6位代码 → 市场前缀"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


def _normalize_code(code: str) -> str:
    """归一化股票代码为纯6位数字"""
    code = code.upper().replace("SH", "").replace("SZ", "").replace("BJ", "")
    code = code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    return code.strip()


def _eastmoney_datacenter(report_name: str, columns: str = "ALL",
                          filter_str: str = "", page_size: int = 50,
                          sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """东财数据中心统一查询"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    try:
        r = _session.get(DATACENTER_URL, params=params,
                         headers={"User-Agent": UA}, timeout=DEFAULT_TIMEOUT)
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except Exception as e:
        print(f"[a-stock] 东财数据中心查询失败: {e}")
    return []


# ============================================================
# Layer 1: 行情层
# ============================================================

def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """腾讯财经实时行情 — PE/PB/市值/换手率/涨跌停"""
    prefixed = []
    for c in codes:
        # 如果调用方已经写了前缀（sh/sz/bj），保留不动
        if re.match(r'^(sh|sz|bj)\d+$', c.lower()):
            prefixed.append(c.lower())
        else:
            code = _normalize_code(c)
            prefix = _get_prefix(code)
            prefixed.append(f"{prefix}{code}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    try:
        _throttle('tencent')  # 腾讯行情 urlopen 自限流
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception as e:
        print(f"[a-stock] 腾讯行情请求失败: {e}")
        return {}

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name": vals[1],
            "price": float(vals[3]) if vals[3] else 0,
            "last_close": float(vals[4]) if vals[4] else 0,
            "open": float(vals[5]) if vals[5] else 0,
            "change_amt": float(vals[31]) if vals[31] else 0,
            "change_pct": float(vals[32]) if vals[32] else 0,
            "high": float(vals[33]) if vals[33] else 0,
            "low": float(vals[34]) if vals[34] else 0,
            "amount_wan": float(vals[37]) if vals[37] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm": float(vals[39]) if vals[39] else 0,
            "amplitude_pct": float(vals[43]) if vals[43] else 0,
            "mcap_yi": float(vals[44]) if vals[44] else 0,
            "float_mcap_yi": float(vals[45]) if vals[45] else 0,
            "pb": float(vals[46]) if vals[46] else 0,
            "limit_up": float(vals[47]) if vals[47] else 0,
            "limit_down": float(vals[48]) if vals[48] else 0,
            "vol_ratio": float(vals[49]) if vals[49] else 0,
            "pe_static": float(vals[52]) if vals[52] else 0,
        }
    return result


def _eastmoney_ulist_quote(codes: list[str]) -> dict[str, dict]:
    """东财 push2 ulist.np 批量实时行情 —— 作为腾讯批量报价的**跨源兜底**(借鉴 portfolio-tracker)。
    一次请求 N 只(secids=市场.代码,1=沪/0=深京)。返回与 _tencent_quote 同构的精简 dict。"""
    secids = []
    for c in codes:
        code = _normalize_code(c)
        mk = '1' if _get_prefix(code) == 'sh' else '0'
        secids.append(f'{mk}.{code}')
    fields = 'f2,f3,f4,f5,f6,f8,f9,f12,f14,f15,f16,f17,f18,f20,f21,f23,f10'
    url = ('https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2'
           f'&ut=bd1d9ddb04089700cf9c27f6f7426281&fields={fields}&secids=' + ','.join(secids))
    try:
        _throttle('eastmoney')
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        raw = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
        diff = (json.loads(raw).get('data') or {}).get('diff') or []
    except Exception as e:
        print(f'[a-stock] 东财 ulist 批量行情失败: {type(e).__name__}')
        return {}

    def _f(v):
        try:
            return float(v) if v not in (None, '', '-') else 0.0
        except (ValueError, TypeError):
            return 0.0

    result = {}
    for r in diff:
        code = str(r.get('f12', ''))
        if not code:
            continue
        result[code] = {
            'name': r.get('f14'), 'price': _f(r.get('f2')), 'last_close': _f(r.get('f18')),
            'open': _f(r.get('f17')), 'change_amt': _f(r.get('f4')), 'change_pct': _f(r.get('f3')),
            'high': _f(r.get('f15')), 'low': _f(r.get('f16')),
            'amount_wan': _f(r.get('f6')) / 1e4, 'turnover_pct': _f(r.get('f8')),
            'pe_ttm': _f(r.get('f9')), 'mcap_yi': _f(r.get('f20')) / 1e8,
            'float_mcap_yi': _f(r.get('f21')) / 1e8, 'pb': _f(r.get('f23')),
            'vol_ratio': _f(r.get('f10')),
        }
    return result


def _batch_quote(codes: list[str]) -> dict[str, dict]:
    """批量实时行情:腾讯主源,缺失的代码用东财 ulist 兜底(跨源容灾)。"""
    out = _tencent_quote(codes)
    missing = [c for c in codes if _normalize_code(c) not in out]
    if missing:
        out.update(_eastmoney_ulist_quote(missing))
    return out


def _baidu_kline_with_ma(code: str, start_time: str = "") -> dict:
    """百度股市通K线 — 自带MA5/MA10/MA20均价"""
    code = _normalize_code(code)
    url = "https://finance.pae.baidu.com/selfselect/getstockquotation"
    params = {
        "all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
        "isFutures": "false", "isStock": "true", "newFormat": "1",
        "group": "quotation_kline_ab", "finClientType": "pc",
        "code": code, "start_time": start_time, "ktype": "1",
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": "https://gushitong.baidu.com",
        "Referer": "https://gushitong.baidu.com/",
    }
    try:
        r = _session.get(url, params=params, headers=headers, timeout=10)
        d = r.json()
        result = d.get("Result", {})
        md = result.get("newMarketData", {})
        keys = md.get("keys", [])
        rows = md.get("marketData", "").split(";")
        return {"keys": keys, "rows": rows}
    except Exception as e:
        print(f"[a-stock] 百度K线请求失败: {e}")
        return {"keys": [], "rows": []}


# ============================================================
# Layer 2: 研报层
# ============================================================

def _eastmoney_reports(code: str, max_pages: int = 3) -> list[dict]:
    """拉取指定股票的研报列表"""
    code = _normalize_code(code)
    REPORT_API = "https://reportapi.eastmoney.com/report/list"
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
    try:
        from rate_limiter import throttled_session
        throttled_session(session)  # 研报分页请求自限流
    except Exception:
        pass
    all_records = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "50", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
        }
        try:
            r = session.get(REPORT_API, params=params, timeout=30)
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            all_records.extend(rows)
            if page >= (d.get("TotalPage", 1) or 1):
                break
        except Exception as e:
            print(f"[a-stock] 研报请求失败: {e}")
            break
    return all_records


def _ths_eps_forecast(code: str) -> pd.DataFrame:
    """同花顺机构一致预期EPS"""
    code = _normalize_code(code)
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {
        "User-Agent": UA,
        "Referer": "https://basic.10jqka.com.cn/",
    }
    try:
        r = _session.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        # ⚠️ pandas 2.1+ 不再接受裸 HTML 字符串(短串会被当文件路径打开,抛 FileNotFoundError
        #    "[Errno 2] No such file or directory: <!DOCTYPE HTML>"), 必须 StringIO 包装。
        dfs = pd.read_html(StringIO(r.text))
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                return df
        return dfs[0] if dfs else pd.DataFrame()
    except ValueError as e:
        # "No tables found" 是该股票无机构一致预期数据(同花顺返回空表), 正常语义不算异常,
        # 静默返回空 DataFrame 避免日志刷屏(34 条/天)。其它 ValueError 仍走下面通用分支。
        if 'No tables found' in str(e):
            return pd.DataFrame()
        print(f"[a-stock] 一致预期({code}) 解析失败: {type(e).__name__}: {str(e)[:120]}")
        return pd.DataFrame()
    except Exception as e:
        print(f"[a-stock] 一致预期({code}) 请求失败: {type(e).__name__}: {str(e)[:120]}")
        return pd.DataFrame()


# ============================================================
# Layer 3: 信号层
# ============================================================

def _ths_hot_reason(date: str = None) -> pd.DataFrame:
    """同花顺当日强势股归因 + 题材标签"""
    if date is None:
        from datetime import date as _date
        date = _date.today().strftime("%Y-%m-%d")

    url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
    headers = {"User-Agent": UA}
    try:
        r = _session.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")
        rows = data.get("data") or []
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        rename_map = {
            "name": "名称", "code": "代码", "reason": "题材归因",
            "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
            "huanshou": "换手率%", "chengjiaoe": "成交额",
            "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
            "market": "市场",
        }
        df = df.rename(columns=rename_map)
        return df
    except Exception as e:
        print(f"[a-stock] 同花顺热点请求失败: {e}")
        return pd.DataFrame()


def _eastmoney_fund_flow_minute(code: str) -> list[dict]:
    """个股资金流向（分钟级，当日盘中）"""
    code = _normalize_code(code)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "secid": secid, "klt": 1,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Origin": "https://quote.eastmoney.com",
    }
    try:
        r = _session.get(url, params=params, headers=headers, timeout=10)
        d = r.json()
    except Exception as e:
        print(f"[a-stock] 资金流(分钟)请求失败: {e}")
        return []

    rows = []
    for line in d.get("data", {}).get("klines", []):
        parts = line.split(",")
        if len(parts) >= 6:
            rows.append({
                "time": parts[0],
                "main_net": float(parts[1]),
                "small_net": float(parts[2]),
                "mid_net": float(parts[3]),
                "large_net": float(parts[4]),
                "super_net": float(parts[5]),
            })
    return rows


def _stock_fund_flow_120d(code: str) -> list[dict]:
    """个股资金流（日级，最近120个交易日）单位: 元"""
    code = _normalize_code(code)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": "120",
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Origin": "https://quote.eastmoney.com",
    }
    try:
        r = _session.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        print(f"[a-stock] 资金流(120日)请求失败: {e}")
        return []

    klines = d.get("data", {}).get("klines", [])
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) >= 7:
            rows.append({
                "date": parts[0],
                "main_net": float(parts[1]) if parts[1] != "-" else 0,
                "small_net": float(parts[2]) if parts[2] != "-" else 0,
                "mid_net": float(parts[3]) if parts[3] != "-" else 0,
                "large_net": float(parts[4]) if parts[4] != "-" else 0,
                "super_net": float(parts[5]) if parts[5] != "-" else 0,
            })
    return rows


def _baidu_concept_blocks(code: str) -> dict:
    """百度股市通概念板块归属"""
    code = _normalize_code(code)
    url = (f"https://finance.pae.baidu.com/api/getrelatedblock"
           f"?code={code}&market=ab"
           f"&typeCode=all&finClientType=pc")
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": "https://gushitong.baidu.com",
        "Referer": "https://gushitong.baidu.com/",
    }
    try:
        r = _session.get(url, headers=headers, timeout=10)
        d = r.json()
        if str(d.get("ResultCode", -1)) != "0":
            return {"industry": [], "concept": [], "region": [], "concept_tags": []}

        result = {"industry": [], "concept": [], "region": [], "concept_tags": []}
        for block in d.get("Result", []):
            block_type = block.get("type", "")
            for item in block.get("list", []):
                entry = {
                    "name": item.get("name", ""),
                    "change_pct": item.get("increase", ""),
                    "desc": item.get("desc", ""),
                }
                if "行业" in block_type:
                    result["industry"].append(entry)
                elif "概念" in block_type:
                    result["concept"].append(entry)
                    result["concept_tags"].append(entry["name"])
                elif "地域" in block_type:
                    result["region"].append(entry)
        return result
    except Exception as e:
        print(f"[a-stock] 概念板块请求失败: {e}")
        return {"industry": [], "concept": [], "region": [], "concept_tags": []}


def _industry_comparison(top_n: int = 20) -> dict:
    """全行业涨跌幅排名（东财行业板块）"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    headers = {"User-Agent": UA}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return {"top": [], "bottom": [], "total": 0}

        rows = []
        for i, item in enumerate(items):
            rows.append({
                "rank": i + 1,
                "name": item.get("f14", ""),
                "change_pct": item.get("f3", 0),
                "code": item.get("f12", ""),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "leader": item.get("f140", ""),
                "leader_change": item.get("f136", 0),
            })
        return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}
    except Exception as e:
        print(f"[a-stock] 行业对比请求失败: {e}")
        return {"top": [], "bottom": [], "total": 0}


def _concept_comparison(top_n: int = 50) -> dict:
    """全概念板块涨跌幅排名（东财 push2，fs=m:90+t:3）

    返回与 _industry_comparison 同结构：{top: [...], bottom: [...], total: N}
    每条 item 字段：rank, name, code, change_pct, up_count, down_count, leader, leader_change
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "300", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:3",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    headers = {"User-Agent": UA}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return {"top": [], "bottom": [], "total": 0}

        rows = []
        for i, item in enumerate(items):
            rows.append({
                "rank": i + 1,
                "name": item.get("f14", ""),
                "change_pct": item.get("f3", 0),
                "code": item.get("f12", ""),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "leader": item.get("f140", ""),
                "leader_change": item.get("f136", 0),
            })
        return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}
    except Exception as e:
        print(f"[a-stock] 概念对比请求失败: {e}")
        return {"top": [], "bottom": [], "total": 0}


def _sector_fund_flow_push2(sector_type: str = "industry", top_n: int = 50) -> list[dict]:
    """行业/概念板块资金流（东财 push2 clist 资金流字段）

    sector_type: 'industry' -> m:90+t:2；'concept' -> m:90+t:3
    返回 list[dict]，按今日主力净流入降序。每条字段：
      name, code, change_pct, main_net_inflow, main_net_inflow_pct,
      super_large_net_inflow, large_net_inflow, medium_net_inflow,
      small_net_inflow, leader, leader_change
    单位：金额为元（不除 1e4），与 akshare 一致
    """
    fs = "m:90+t:2" if sector_type == "industry" else "m:90+t:3"
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "300", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fid": "f62", "fs": fs,
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205",
    }
    headers = {"User-Agent": UA}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return []

        rows = []
        for item in items[:top_n]:
            rows.append({
                "name": item.get("f14", ""),
                "code": item.get("f12", ""),
                "change_pct": item.get("f3", 0),
                "main_net_inflow": item.get("f62", 0),
                "main_net_inflow_pct": item.get("f184", 0),
                "super_large_net_inflow": item.get("f66", 0),
                "large_net_inflow": item.get("f72", 0),
                "medium_net_inflow": item.get("f78", 0),
                "small_net_inflow": item.get("f84", 0),
                "leader": item.get("f204", ""),
                "leader_change": item.get("f205", 0),
            })
        return rows
    except Exception as e:
        print(f"[a-stock] 板块资金流请求失败({sector_type}): {e}")
        return []


def _dragon_tiger_board(code: str, trade_date: str, look_back: int = 30) -> dict:
    """龙虎榜数据聚合"""
    code = _normalize_code(code)
    start = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)
    start_str = start.strftime("%Y-%m-%d")

    records = []
    data = _eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{start_str}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
        page_size=50, sort_columns="TRADE_DATE", sort_types="-1",
    )
    for row in data:
        records.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "reason": row.get("EXPLANATION", ""),
            "net_buy": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
            "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
        })

    seats = {"buy": [], "sell": []}
    if records:
        latest_date = records[0]["date"]
        buy_data = _eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="BUY", sort_types="-1",
        )
        for row in buy_data[:5]:
            seats["buy"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                "net": round((row.get("NET") or 0) / 10000, 1),
            })
        sell_data = _eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="SELL", sort_types="-1",
        )
        for row in sell_data[:5]:
            seats["sell"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                "net": round((row.get("NET") or 0) / 10000, 1),
            })

    institution = {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}
    for detail_data, side in [(buy_data, "buy"), (sell_data, "sell")]:
        for row in detail_data:
            if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                amt = (row.get("BUY") or 0) if side == "buy" else (row.get("SELL") or 0)
                if side == "buy":
                    institution["buy_amt"] += amt
                else:
                    institution["sell_amt"] += amt
    institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
    institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
    institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)

    return {"records": records, "seats": seats, "institution": institution}


# ============================================================
# Layer 4: 资金面 / 筹码层
# ============================================================

def _margin_trading(code: str, page_size: int = 30) -> list[dict]:
    """融资融券明细（日级）"""
    code = _normalize_code(code)
    data = _eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=page_size, sort_columns="DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
            "rzye": row.get("RZYE", 0),
            "rzmre": row.get("RZMRE", 0),
            "rzche": row.get("RZCHE", 0),
            "rqye": row.get("RQYE", 0),
            "rzrqye": row.get("RZRQYE", 0),
        })
    return rows


def _block_trade(code: str, page_size: int = 20) -> list[dict]:
    """大宗交易记录"""
    code = _normalize_code(code)
    data = _eastmoney_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        close = row.get("CLOSE_PRICE") or 0
        deal_price = row.get("DEAL_PRICE") or 0
        premium = ((deal_price / close - 1) * 100) if close else 0
        rows.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "price": deal_price,
            "close": close,
            "premium_pct": round(premium, 2),
            "vol": row.get("DEAL_VOLUME", 0),
            "amount": row.get("DEAL_AMT", 0),
            "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        })
    return rows


def _holder_num_change(code: str, page_size: int = 10) -> list[dict]:
    """股东户数变化（季度级）"""
    code = _normalize_code(code)
    data = _eastmoney_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size, sort_columns="END_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("END_DATE", ""))[:10],
            "holder_num": row.get("HOLDER_NUM", 0),
            "change_num": row.get("HOLDER_NUM_CHANGE", 0),
            "change_ratio": row.get("HOLDER_NUM_RATIO", 0),
            "avg_shares": row.get("AVG_FREE_SHARES", 0),
        })
    return rows


def _dividend_history(code: str, page_size: int = 20) -> list[dict]:
    """分红送转历史"""
    code = _normalize_code(code)
    data = _eastmoney_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("EX_DIVIDEND_DATE", ""))[:10],
            "bonus_rmb": row.get("PRETAX_BONUS_RMB", 0),
            "transfer_ratio": row.get("TRANSFER_RATIO", 0),
            "bonus_ratio": row.get("BONUS_RATIO", 0),
            "plan": row.get("ASSIGN_PROGRESS", ""),
        })
    return rows


# ============================================================
# Layer 5: 新闻层
# ============================================================

def _eastmoney_stock_news(code: str, page_size: int = 20) -> list[dict]:
    """东财个股新闻"""
    code = _normalize_code(code)
    cb = "jQuery_news"
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner_params = json.dumps({
        "uid": "", "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                  "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": ""}},
    }, separators=(',', ':'))
    params = {"cb": cb, "param": inner_params}
    headers = {"User-Agent": UA, "Referer": "https://so.eastmoney.com/"}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=15)
        text = r.text
        json_str = text[text.index("(") + 1: text.rindex(")")]
        d = json.loads(json_str)
        rows = []
        result = d.get("result", {})
        # cmsArticleWebOld 可能是 dict {list: [...]} 或直接是 list [...]
        article_container = result.get("cmsArticleWebOld", [])
        if isinstance(article_container, dict):
            articles = article_container.get("list", [])
        elif isinstance(article_container, list):
            articles = article_container
        else:
            articles = []
        for a in articles:
            if not isinstance(a, dict):
                continue
            rows.append({
                "title": re.sub(r'<[^>]+>', '', a.get("title", "")),
                "content": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                "time": a.get("date", ""),
                "source": a.get("mediaName", ""),
                "url": a.get("url", ""),
            })
        return rows
    except Exception as e:
        print(f"[a-stock] 个股新闻请求失败: {e}")
        return []


def _cls_telegraph(page_size: int = 50) -> list[dict]:
    """财联社电报（全市场实时快讯）"""
    url = "https://www.cls.cn/nodeapi/telegraphList"
    params = {"rn": str(page_size), "page": "1"}
    headers = {"User-Agent": UA, "Referer": "https://www.cls.cn/"}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=10)
        d = r.json()
        rows = []
        for item in d.get("data", {}).get("roll_data", []):
            rows.append({
                "title": item.get("title", "") or item.get("brief", ""),
                "content": item.get("content", "") or item.get("brief", ""),
                "time": item.get("ctime", ""),
            })
        return rows
    except Exception as e:
        print(f"[a-stock] 财联社快讯请求失败: {e}")
        return []


# ============================================================
# Layer 6: 基础数据
# ============================================================

def _eastmoney_stock_info(code: str) -> dict:
    """东财个股基本面信息"""
    code = _normalize_code(code)
    market_code = 1 if code.startswith("6") else 0
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "fltt": "2", "invt": "2",
        "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
        "secid": f"{market_code}.{code}",
    }
    headers = {"User-Agent": UA}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=10)
        d = r.json().get("data", {})
        return {
            "code": d.get("f57", ""),
            "name": d.get("f58", ""),
            "industry": d.get("f127", ""),
            "total_shares": d.get("f84", 0),
            "float_shares": d.get("f85", 0),
            "mcap": d.get("f116", 0),
            "float_mcap": d.get("f117", 0),
            "list_date": str(d.get("f189", "")),
            "price": d.get("f43", 0),
        }
    except Exception as e:
        print(f"[a-stock] 东财个股信息请求失败: {e}")
        return {}


def _sina_financial_report(code: str, report_type: str = "lrb") -> list[dict]:
    """新浪财报三表"""
    code = _normalize_code(code)
    prefix = "sh" if code.startswith("6") else "sz"
    paper_code = f"{prefix}{code}"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code,
        "source": report_type,
        "type": "0", "page": "1", "num": "20",
    }
    headers = {"User-Agent": UA}
    try:
        r = _session.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        result = d.get("result", {}).get("data", {})
        items = result.get(report_type, [])
        return items if isinstance(items, list) else []
    except Exception as e:
        print(f"[a-stock] 新浪财报请求失败: {e}")
        return []


def _lockup_expiry(code: str, trade_date: str, forward_days: int = 90) -> dict:
    """限售解禁日历"""
    code = _normalize_code(code)
    history_data = _eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=15, sort_columns="FREE_DATE", sort_types="-1",
    )
    history = []
    for row in history_data:
        history.append({
            "date": str(row.get("FREE_DATE", ""))[:10],
            "type": row.get("LIMITED_STOCK_TYPE", ""),
            "shares": row.get("FREE_SHARES_NUM", 0),
            "ratio": row.get("FREE_RATIO", 0),
        })

    end_date = datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=forward_days)
    end_str = end_date.strftime("%Y-%m-%d")
    upcoming_data = _eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")(FREE_DATE>="{trade_date}")(FREE_DATE<="{end_str}")',
        page_size=20, sort_columns="FREE_DATE", sort_types="1",
    )
    upcoming = []
    for row in upcoming_data:
        upcoming.append({
            "date": str(row.get("FREE_DATE", ""))[:10],
            "type": row.get("LIMITED_STOCK_TYPE", ""),
            "shares": row.get("FREE_SHARES_NUM", 0),
            "ratio": row.get("FREE_RATIO", 0),
        })

    return {"history": history, "upcoming": upcoming}


# ============================================================
# 公告层
# ============================================================

def _cninfo_announcements(code: str, page_size: int = 30) -> list[dict]:
    """巨潮公告全文检索"""
    code = _normalize_code(code)
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    if code.startswith("6"):
        org_id = f"gssh0{code}"
    elif code.startswith("8") or code.startswith("4"):
        org_id = f"gsbj0{code}"
    else:
        org_id = f"gssz0{code}"

    payload = {
        "stock": f"{code},{org_id}",
        "tabName": "fulltext",
        "pageSize": str(page_size),
        "pageNum": "1",
        "column": "", "category": "", "plate": "",
        "seDate": "", "searchkey": "", "secid": "",
        "sortName": "", "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.cninfo.com.cn/new/disclosure",
        "Origin": "https://www.cninfo.com.cn",
    }
    try:
        r = _session.post(url, data=payload, headers=headers, timeout=15)
        d = r.json()
        rows = []
        for item in d.get("announcements", []) or []:
            rows.append({
                "title": item.get("announcementTitle", ""),
                "type": item.get("announcementTypeName", ""),
                "date": item.get("announcementTime", ""),
                "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={item.get('announcementId', '')}",
            })
        return rows
    except Exception as e:
        print(f"[a-stock] 巨潮公告请求失败: {e}")
        return []


# ============================================================
# 估值计算
# ============================================================

def _forward_pe(price: float, eps_forecast: float) -> float:
    if eps_forecast <= 0:
        return float("inf")
    return price / eps_forecast


def _calc_peg(pe: float, cagr: float) -> float:
    if cagr <= 0:
        return float("inf")
    return pe / (cagr * 100)


def _full_valuation(code: str) -> dict:
    """单票完整估值分析"""
    code = _normalize_code(code)

    # 腾讯实时行情
    quotes = _tencent_quote([code])
    q = quotes.get(code, {})
    price = q.get("price", 0)

    # 同花顺一致预期
    df = _ths_eps_forecast(code)
    eps_cur = eps_next = None
    analyst_count = 0
    if not df.empty and len(df.columns) >= 3:
        try:
            for i, row in df.iterrows():
                if i == 0:
                    eps_cur = float(row.iloc[2]) if pd.notna(row.iloc[2]) else None
                    analyst_count = int(row.iloc[1]) if pd.notna(row.iloc[1]) else 0
                elif i == 1:
                    eps_next = float(row.iloc[2]) if pd.notna(row.iloc[2]) else None
        except (ValueError, IndexError):
            pass

    pe_fwd = price / eps_cur if (eps_cur and price) else float("inf")
    cagr = (eps_next / eps_cur - 1) if (eps_cur and eps_next and eps_cur > 0) else 0
    peg = _calc_peg(pe_fwd, cagr) if cagr > 0 else float("inf")
    digest = (
        math.log(pe_fwd / 30) / math.log(1 + cagr)
        if (pe_fwd > 30 and cagr > 0 and pe_fwd != float("inf")) else 0
    )

    return {
        "name": q.get("name", ""),
        "price": price,
        "mcap_yi": q.get("mcap_yi", 0),
        "pe_ttm": q.get("pe_ttm", 0),
        "pb": q.get("pb", 0),
        "eps_cur": eps_cur,
        "eps_next": eps_next,
        "pe_fwd": round(pe_fwd, 1) if eps_cur else None,
        "cagr_pct": round(cagr * 100, 0) if cagr else None,
        "peg": round(peg, 2) if peg != float("inf") else None,
        "digest_years": round(digest, 1),
        "analyst_count": analyst_count,
    }


# ============================================================
# 统一接口类
# ============================================================

class AStockDataAdapter:
    """
    A股全栈数据适配器 — 统一接口

    所有方法的 symbol 参数：
    - 纯6位数字: "688017"
    - 带前缀: "SH688017", "sz000001"
    - 带后缀: "688017.SH", "000001.SZ"
    """

    # ─── 行情 ──────────────────────────────────────────

    def get_quote(self, symbol: str) -> dict:
        """获取实时行情（含PE/PB/市值/换手率/涨跌停）。腾讯主源 + 东财 ulist 兜底。"""
        result = _batch_quote([symbol])
        return result.get(_normalize_code(symbol), {})

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """批量获取实时行情(腾讯主源,缺失代码用东财 ulist 跨源兜底)。"""
        return _batch_quote(symbols)

    def get_quote_dataframe(self, symbols: list[str]) -> pd.DataFrame:
        """批量获取行情并返回DataFrame"""
        data = _batch_quote(symbols)
        if not data:
            return pd.DataFrame()
        rows = []
        for code, vals in data.items():
            vals["symbol"] = code
            rows.append(vals)
        return pd.DataFrame(rows)

    def get_baidu_kline_ma(self, symbol: str) -> dict:
        """获取K线 + 自带MA5/MA10/MA20"""
        return _baidu_kline_with_ma(symbol)

    # ─── 研报 ──────────────────────────────────────────

    def get_reports(self, symbol: str, max_pages: int = 3) -> list[dict]:
        """获取个股研报列表"""
        return _eastmoney_reports(symbol, max_pages)

    def get_eps_forecast(self, symbol: str) -> pd.DataFrame:
        """获取同花顺机构一致预期EPS"""
        return _ths_eps_forecast(symbol)

    def get_full_valuation(self, symbol: str) -> dict:
        """完整估值 — 前向PE / PEG / PE消化年数（30x 锚点）

        基于 a-stock-data 估值框架：
          - 数据：腾讯实时行情（price/pe_ttm/pb/mcap）+ 同花顺一致预期EPS
          - 公式：pe_fwd = price/eps_cur；cagr = eps_next/eps_cur-1；
                  peg = pe_fwd/(cagr*100)；digest = log(pe_fwd/30)/log(1+cagr)
        判断: PEG<1 便宜 / 1-1.5 合理 / >1.5 贵；消化年数 <2 合理 / >4 太贵
        """
        return _full_valuation(symbol)

    # ─── 信号层 ───────────────────────────────────────

    def get_hot_stocks(self, date: str = None) -> pd.DataFrame:
        """获取当日强势股 + 题材归因"""
        return _ths_hot_reason(date)

    def get_fund_flow_minute(self, symbol: str) -> list[dict]:
        """当日盘中分钟级资金流向"""
        return _eastmoney_fund_flow_minute(symbol)

    def get_fund_flow_history(self, symbol: str, days: int = 120) -> list[dict]:
        """历史资金流向（日级，默认120日）"""
        return _stock_fund_flow_120d(symbol)

    def get_concept_blocks(self, symbol: str) -> dict:
        """获取概念板块归属（行业/概念/地域）"""
        return _baidu_concept_blocks(symbol)

    def get_industry_ranking(self, top_n: int = 20) -> dict:
        """获取行业板块排名（东财 push2 m:90+t:2）"""
        return _industry_comparison(top_n)

    def get_concept_ranking(self, top_n: int = 50) -> dict:
        """获取概念板块排名（东财 push2 m:90+t:3，零鉴权替代 akshare）"""
        return _concept_comparison(top_n)

    def get_sector_fund_flow(self, sector_type: str = "industry", top_n: int = 50) -> list[dict]:
        """获取行业/概念板块资金流（东财 push2，零鉴权替代 akshare）

        sector_type: 'industry' 行业 / 'concept' 概念
        """
        return _sector_fund_flow_push2(sector_type, top_n)

    def get_dragon_tiger(self, symbol: str, trade_date: str = None, look_back: int = 30) -> dict:
        """龙虎榜数据"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        return _dragon_tiger_board(symbol, trade_date, look_back)

    # ─── 资金面 / 筹码层 ──────────────────────────────

    def get_margin_trading(self, symbol: str, page_size: int = 30) -> list[dict]:
        """融资融券明细"""
        return _margin_trading(symbol, page_size)

    def get_block_trade(self, symbol: str, page_size: int = 20) -> list[dict]:
        """大宗交易"""
        return _block_trade(symbol, page_size)

    def get_holder_num_change(self, symbol: str, page_size: int = 10) -> list[dict]:
        """股东户数变化"""
        return _holder_num_change(symbol, page_size)

    def get_dividend_history(self, symbol: str, page_size: int = 20) -> list[dict]:
        """分红送转历史"""
        return _dividend_history(symbol, page_size)

    def get_lockup_expiry(self, symbol: str, trade_date: str = None, forward_days: int = 90) -> dict:
        """限售解禁日历"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        return _lockup_expiry(symbol, trade_date, forward_days)

    # ─── 新闻 ──────────────────────────────────────────

    def get_stock_news(self, symbol: str, page_size: int = 20) -> list[dict]:
        """个股新闻"""
        return _eastmoney_stock_news(symbol, page_size)

    def get_market_news(self, page_size: int = 50) -> list[dict]:
        """财联社全市场快讯"""
        return _cls_telegraph(page_size)

    # ─── 基础数据 ─────────────────────────────────────

    def get_stock_info(self, symbol: str) -> dict:
        """获取个股基本面信息（行业/股本/市值/上市日期）"""
        return _eastmoney_stock_info(symbol)

    def get_stock_info_detailed(self, symbol: str) -> dict:
        """获取个股详细信息（行情 + 基本面合并）

        东财 push2 接口在某些网络环境（Windows + 代理/TLS 指纹问题）下会失败，
        所以从 quote (腾讯接口) 兜底取 name / industry，保证最关键字段不空。
        """
        code = _normalize_code(symbol)
        quote = self.get_quote(code)
        info = self.get_stock_info(code)
        # name 优先用 info（东财，更权威），失败时回退到 quote（腾讯）
        merged_name = info.get('name') or quote.get('name') or ''
        info.update({
            "name": merged_name,
            "price": quote.get("price", 0),
            "pe_ttm": quote.get("pe_ttm", 0),
            "pb": quote.get("pb", 0),
            "turnover_pct": quote.get("turnover_pct", 0),
            "mcap_yi": quote.get("mcap_yi", 0),
            "change_pct": quote.get("change_pct", 0),
        })
        return info

    def get_financial_reports(self, symbol: str, report_type: str = "lrb") -> list[dict]:
        """新浪财报三表: fzb=资产负债表 lrb=利润表 llb=现金流量表"""
        return _sina_financial_report(symbol, report_type)

    # ─── 公告 ──────────────────────────────────────────

    def get_announcements(self, symbol: str, page_size: int = 30) -> list[dict]:
        """巨潮公告检索"""
        return _cninfo_announcements(symbol, page_size)

    # ─── 估值 ──────────────────────────────────────────

    def full_valuation(self, symbol: str) -> dict:
        """单票完整估值分析（30秒快速调研）"""
        return _full_valuation(symbol)


# ============================================================
# 单例
# ============================================================
adapter = AStockDataAdapter()

# ============================================================
# 简易测试
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("A 股全栈数据适配器 V1.0")
    print("=" * 60)

    # 行情测试
    print("\n📊 行情测试 (600519 贵州茅台):")
    info = adapter.get_stock_info_detailed("600519")
    for k, v in info.items():
        print(f"  {k}: {v}")

    print("\n📈 批量行情:")
    q = adapter.get_quotes(["688017", "300476", "600519"])
    for c, d in q.items():
        print(f"  {c}: {d.get('name', '')} 现价{d.get('price', 0)} PE={d.get('pe_ttm', 0)}")

    print("\n🔥 概念板块 (688017):")
    blocks = adapter.get_concept_blocks("688017")
    print(f"  概念: {blocks['concept_tags']}")

    print("\n💰 资金流120日 (600519):")
    flow = adapter.get_fund_flow_history("600519")
    if flow:
        print(f"  最近5日:")
        for d in flow[-5:]:
            print(f"  {d['date']}: 主力{d['main_net']/1e4:.0f}万 超大单{d['super_net']/1e4:.0f}万")

    print("\n✅ 适配器加载完成")
