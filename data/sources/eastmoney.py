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

from typing import List, Optional

import pandas as pd

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
    print('OK' if (n and cb and not fn.empty) else '⚠️ 部分能力空(可能网络/被封)')
