# -*- coding: utf-8 -*-
"""data.sources.eastmoney —— 东方财富直连原子源(阶段 2 子集)。

直连东财公开 HTTP 接口,**不碰 akshare**。归一工具从 data/sources/_common.py 取;契约见 README.md。

本阶段落地东财域中**原先走 akshare 的三项**(其余东财能力如行情/K线/龙虎榜/datacenter 多数已在
a_stock_data_adapter 直连,阶段 3 再归位本模块):
  · global_news(page_size)  —— 全球财经快讯(替 ak.stock_info_global_em;np-weblist getFastNewsList)。
  · convertible_bonds()     —— 可转债比价表(替 ak.bond_cov_comparison;push2 clist b:MK0354,
        **按 f-code 直连映射**,不依赖 akshare 脆弱的位置列名)。
  · fund_nav(code)          —— 基金历史净值(东财 f10/lsjz JSON,纯 HTTP;原 datahub._fund_nav_eastmoney 归位)。

契约铁律:异常吞掉返空(空 DataFrame / [])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
import requests

from . import _common as C


# ── 全球财经快讯 ─────────────────────────────────────────────────────────────
_GNEWS_URL = ("https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
              "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={ps}&req_trace=1")


def global_news(page_size: int = 50) -> List[dict]:
    """东财全球财经快讯 → [{title, content, time, url}](新→旧)。空/异常 → []。
    与 ak.stock_info_global_em() + datahub._news_em 逐字段一致(content=摘要,url=finance.eastmoney 文章页)。"""
    try:
        C.throttle('eastmoney')
        d = C.http_get_json(_GNEWS_URL.format(ps=max(int(page_size), 1)), timeout=10)
        lst = (((d or {}).get('data') or {}).get('fastNewsList')) or []
        out = []
        for it in lst[:page_size]:
            code = str(it.get('code', ''))
            out.append({
                'title': str(it.get('title', '')),
                'content': str(it.get('summary', '')),
                'time': str(it.get('showTime', '')),
                'url': f'https://finance.eastmoney.com/a/{code}.html' if code else '',
            })
        return out
    except Exception:
        return []


# ── 可转债比价表 ─────────────────────────────────────────────────────────────
# push2 clist,板块 b:MK0354=可转债。f-code 已对照 akshare 验证(2026-06-27):
#   f12=转债代码 f14=转债名称 f2=转债最新价 f3=转债涨跌幅 f237=转股溢价率 f236=转股价值
#   f232=正股代码 f234=正股名称。东财比价表无评级/到期收益/剩余年限/规模/换手 → None(由集思录补)。
_CB_FIELDS = ("f1,f152,f2,f3,f12,f13,f14,f227,f228,f229,f230,f231,f232,f233,f234,"
              "f235,f236,f237,f238,f239,f240,f241,f242,f26,f243")
_CB_URL = ("https://16.push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1"
           "&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f243&fs=b:MK0354&fields=" + _CB_FIELDS)


def _cb_num(v):
    """数值归一(与 datahub._cb_num 同口径):float→round3,'-'/NaN/异常→None。"""
    try:
        f = float(v)
        return round(f, 3) if f == f else None
    except Exception:
        return None


def _cb_row(r: dict) -> dict:
    price = _cb_num(r.get('f2'))
    prem = _cb_num(r.get('f237'))
    return {
        'code': str(r.get('f12', '')), 'name': str(r.get('f14', '')),
        'price': price, 'change_pct': _cb_num(r.get('f3')),
        'premium_pct': prem, 'conv_value': _cb_num(r.get('f236')),
        'double_low': round(price + prem, 2) if (price is not None and prem is not None) else None,
        'rating': '', 'stock_code': str(r.get('f232', '')), 'stock_name': str(r.get('f234', '')),
        'ytm_pct': None, 'remain_years': None, 'remain_scale_yi': None, 'turnover_pct': None,
    }


def convertible_bonds() -> List[dict]:
    """全市场可转债比价(双低策略用)→ list[dict](键见 datahub.convertible_bonds)。空/异常 → 已收集的部分。
    分页拉全(pz=100,~3-4 页);评级/到期收益/剩余年限/规模/换手 东财比价表不含 → None。"""
    out: List[dict] = []
    try:
        total = None
        pz = 100
        for pn in range(1, 12):
            C.throttle('eastmoney')
            d = C.http_get_json(_CB_URL.format(pn=pn, pz=pz), timeout=12)
            data = (d or {}).get('data') or {}
            diff = data.get('diff') or []
            if isinstance(diff, dict):      # 老接口 diff 为 dict
                diff = list(diff.values())
            if total is None:
                total = int(data.get('total') or 0)
            if not diff:
                break
            for r in diff:
                out.append(_cb_row(r))
            if len(out) >= total or len(diff) < pz:
                break
    except Exception:
        return out   # 中途失败返回已收集部分(非空胜空)
    return out


# ── 基金历史净值 ─────────────────────────────────────────────────────────────
_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex={page}&pageSize={ps}"


def fund_nav(code: str) -> pd.DataFrame:
    """东财 f10/lsjz JSON 翻页拉全部历史净值(纯 HTTP,无 JS exec)。
    返回标准列 DataFrame[date, unit_nav, acc_nav, daily_return] 升序;空/异常 → 空 DF。
    ⚠️ lsjz 必须带 Referer(否则返空,实测)。"""
    code = str(code).zfill(6)
    ref = f'http://fundf10.eastmoney.com/jjjz_{code}.html'
    rows: List[dict] = []
    ps = 200
    try:
        for page in range(1, 200):     # 200*200=40000 条,远超任何基金历史
            C.throttle('eastmoney')
            d = C.http_get_json(_LSJZ_URL.format(code=code, page=page, ps=ps),
                                headers={'Referer': ref}, timeout=8)
            lst = ((d.get('Data') or {}).get('LSJZList')) or []
            if not lst:
                break
            rows.extend(lst)
            if len(lst) < ps:
                break
    except Exception:
        if not rows:
            return pd.DataFrame()
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


# ── 东财数据中心(datacenter:个股公司数据)───────────────────────────────────
# 共享:trust_env=False 限流 requests 会话(国内源不走代理,与原 adapter 同口径)+ datacenter 统一查询。
_DC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SESSION = requests.Session()
_SESSION.trust_env = False
try:
    from rate_limiter import throttled_session as _throttled_session
    _throttled_session(_SESSION)   # 按 host 自动限流(.get 已被包装)
except Exception:
    pass


def datacenter(report_name: str, columns: str = "ALL", filter_str: str = "",
               page_size: int = 50, sort_columns: str = "", sort_types: str = "-1") -> List[dict]:
    """东财数据中心统一查询 → list[dict](原始字段名)。失败 → []。"""
    params = {
        "reportName": report_name, "columns": columns, "filter": filter_str,
        "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    try:
        r = _SESSION.get(_DC_URL, params=params, headers={"User-Agent": _DC_UA}, timeout=15)
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except Exception as e:
        print(f"[sources.eastmoney] 数据中心查询失败: {e}")
    return []


def margin(code: str, page_size: int = 30) -> List[dict]:
    """融资融券明细(日级)→ [{date,rzye,rzmre,rzche,rqye,rzrqye}]。"""
    rows = []
    for row in datacenter("RPTA_WEB_RZRQ_GGMX", filter_str=f'(SCODE="{C.norm_code(code)}")',
                          page_size=page_size, sort_columns="DATE", sort_types="-1"):
        rows.append({
            "date": str(row.get("DATE", ""))[:10], "rzye": row.get("RZYE", 0),
            "rzmre": row.get("RZMRE", 0), "rzche": row.get("RZCHE", 0),
            "rqye": row.get("RQYE", 0), "rzrqye": row.get("RZRQYE", 0),
        })
    return rows


def block_trade(code: str, page_size: int = 20) -> List[dict]:
    """大宗交易记录 → [{date,price,close,premium_pct,vol,amount,buyer,seller}]。"""
    rows = []
    for row in datacenter("RPT_DATA_BLOCKTRADE", filter_str=f'(SECURITY_CODE="{C.norm_code(code)}")',
                          page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1"):
        close = row.get("CLOSE_PRICE") or 0
        deal_price = row.get("DEAL_PRICE") or 0
        premium = ((deal_price / close - 1) * 100) if close else 0
        rows.append({
            "date": str(row.get("TRADE_DATE", ""))[:10], "price": deal_price, "close": close,
            "premium_pct": round(premium, 2), "vol": row.get("DEAL_VOLUME", 0),
            "amount": row.get("DEAL_AMT", 0), "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        })
    return rows


def holder_num_change(code: str, page_size: int = 10) -> List[dict]:
    """股东户数变化(季度级)→ [{date,holder_num,change_num,change_ratio,avg_shares}]。"""
    rows = []
    for row in datacenter("RPT_HOLDERNUMLATEST", filter_str=f'(SECURITY_CODE="{C.norm_code(code)}")',
                          page_size=page_size, sort_columns="END_DATE", sort_types="-1"):
        rows.append({
            "date": str(row.get("END_DATE", ""))[:10], "holder_num": row.get("HOLDER_NUM", 0),
            "change_num": row.get("HOLDER_NUM_CHANGE", 0), "change_ratio": row.get("HOLDER_NUM_RATIO", 0),
            "avg_shares": row.get("AVG_FREE_SHARES", 0),
        })
    return rows


def dividend(code: str, page_size: int = 20) -> List[dict]:
    """分红送转历史 → [{date,bonus_rmb,transfer_ratio,bonus_ratio,plan}]。"""
    rows = []
    for row in datacenter("RPT_SHAREBONUS_DET", filter_str=f'(SECURITY_CODE="{C.norm_code(code)}")',
                          page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1"):
        rows.append({
            "date": str(row.get("EX_DIVIDEND_DATE", ""))[:10], "bonus_rmb": row.get("PRETAX_BONUS_RMB", 0),
            "transfer_ratio": row.get("TRANSFER_RATIO", 0), "bonus_ratio": row.get("BONUS_RATIO", 0),
            "plan": row.get("ASSIGN_PROGRESS", ""),
        })
    return rows


def lockup_expiry(code: str, trade_date: str, forward_days: int = 90) -> dict:
    """限售解禁日历 → {history:[...], upcoming:[...]}(各项 {date,type,shares,ratio})。"""
    c = C.norm_code(code)

    def _rows(data):
        return [{"date": str(r.get("FREE_DATE", ""))[:10], "type": r.get("LIMITED_STOCK_TYPE", ""),
                 "shares": r.get("FREE_SHARES_NUM", 0), "ratio": r.get("FREE_RATIO", 0)} for r in data]

    history = _rows(datacenter("RPT_LIFT_STAGE", filter_str=f'(SECURITY_CODE="{c}")',
                               page_size=15, sort_columns="FREE_DATE", sort_types="-1"))
    end_str = (datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=forward_days)).strftime("%Y-%m-%d")
    upcoming = _rows(datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{c}")(FREE_DATE>="{trade_date}")(FREE_DATE<="{end_str}")',
        page_size=20, sort_columns="FREE_DATE", sort_types="1"))
    return {"history": history, "upcoming": upcoming}


# ── K线(push2his 日线)─────────────────────────────────────────────────────
_KLINE_URL = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?'
              'secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57'
              '&klt=101&fqt={fqt}&end=20500101&lmt={lmt}')
_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095, "5y": 1825}


def _lmt(period: str) -> int:
    """period → 取数根数(自然日→交易日约 ×0.72,多取 30 根冗余)。"""
    return int(_PERIOD_DAYS.get(period, 365) * 0.72) + 30


def kline(code: str, period: str = "1y", interval: str = "1d", adjust: str = "raw") -> pd.DataFrame:
    """东财 push2his 日线。adjust='raw'→fqt=0(不复权,与新浪主源同口径)/ 'qfq'→fqt=1(前复权)。
    返回项目契约(DatetimeIndex='Date' + 大写 OCHLV,Volume「股」)或空 DF。仅日线。
    ⚠️ 指数代码与个股重码 → 放弃(交指数专路);成交量「手」×100 对齐「股」;解析<80% 视残缺弃用。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    c = C.norm_code(code)
    if c in C.EM_INDEX_CODES:
        return pd.DataFrame()
    fqt = '1' if str(adjust) == 'qfq' else '0'   # raw 缓存须 fqt=0,否则历史价跳变污染
    url = _KLINE_URL.format(secid=C.em_secid(c), fqt=fqt, lmt=_lmt(period))
    try:
        C.throttle('eastmoney')
        d = C.http_get_json(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=6)  # 6s 短超时,死源快失败
        klines = ((d.get('data') or {}).get('klines')) or []
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
            # 东财成交量「手」(100 股)→ ×100 对齐新浪主源「股」口径
            rows.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]) * 100))
        except (ValueError, IndexError):
            continue
    # 解析完整性护栏:成功行 < 收到行 80% → 视残缺弃用(防残缺数据挤掉更完整的主源)
    if not rows or len(rows) < len(klines) * 0.8:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=['Date', 'Open', 'Close', 'High', 'Low', 'Volume'])
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    return df.dropna(subset=['Date']).set_index('Date').sort_index()


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.eastmoney 直连自检 ===')
    n = global_news(5)
    print(f'global_news: {len(n)} 条;', (n[0] if n else None))
    cb = convertible_bonds()
    print(f'convertible_bonds: {len(cb)} 只;', (cb[0] if cb else None))
    fn = fund_nav('000001')
    print(f'fund_nav 000001: {len(fn)} 行;', (fn.tail(1).to_string() if not fn.empty else 'EMPTY'))
    kr = kline('600519', '6mo', adjust='raw')
    kq = kline('600519', '6mo', adjust='qfq')
    print(f'kline raw {len(kr)} 行 / qfq {len(kq)} 行; raw末收={kr["Close"].iloc[-1] if len(kr) else None}')
    print('OK' if (n and cb and not fn.empty and len(kr)) else '⚠️ 部分能力空(可能网络/被封)')
