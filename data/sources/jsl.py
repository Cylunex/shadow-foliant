# -*- coding: utf-8 -*-
"""data.sources.jsl —— 集思录(jisilu)可转债直连原子源(阶段 2)。

直连集思录 cb_list_new POST 接口,**不碰 akshare**。契约见 data/sources/README.md。
仅一项能力:convertible_bonds()(全市场可转债比价,convertible_bonds 域的兜底源,东财比价表挂时顶上)。

⚠️ 匿名访问约 30 只(集思录对未登录限量;rp=50 请求但服务端只回约 30)。全量须带 jsl 账号 cookie,
   本项目不持账号 → 作东财(_cb_eastmoney,全市场 ~326)的小样兜底足够。

契约铁律:异常吞掉返空([])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import List

from . import _common as C

_URL = "https://www.jisilu.cn/data/cbnew/cb_list_new/"
_HEADERS = {
    "Referer": "https://www.jisilu.cn/data/cbnew/",
    "Origin": "https://www.jisilu.cn",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
}
# 与 akshare bond_cb_jsl 同口径的查询体(listed=Y 已上市;is_search=N 返全集)。
_PAYLOAD = {
    "fprice": "", "tprice": "", "curr_iss_amt": "", "volume": "", "svolume": "",
    "premium_rt": "", "ytm_rt": "", "market": "", "rating_cd": "", "is_search": "N",
    "market_cd[]": "szcy", "btype": "", "listed": "Y", "qflag": "N",
    "sw_cd": "", "bond_ids": "", "rp": "50",
}


def _num(v):
    """数值归一(与 datahub._cb_num 同口径):float→round3,'-'/NaN/异常→None。"""
    try:
        f = float(v)
        return round(f, 3) if f == f else None
    except Exception:
        return None


def convertible_bonds() -> List[dict]:
    """集思录可转债 → list[dict](键见 datahub.convertible_bonds)。匿名约 30 只。空/异常 → []。"""
    try:
        C.throttle('jisilu')
        params = urllib.parse.urlencode({"___jsl": "LST___t=1"})
        body = json.dumps(_PAYLOAD).encode('utf-8')
        h = {'User-Agent': C._DEFAULT_UA}
        h.update(_HEADERS)
        req = urllib.request.Request(_URL + '?' + params, data=body, headers=h, method='POST')
        raw = urllib.request.urlopen(req, timeout=12).read().decode('utf-8', 'replace')
        rows = (json.loads(raw) or {}).get('rows') or []
    except Exception:
        return []
    out = []
    for it in rows:
        r = (it or {}).get('cell') or {}
        price = _num(r.get('price'))
        prem = _num(r.get('premium_rt'))
        dl = _num(r.get('dblow'))
        if dl is None and price is not None and prem is not None:
            dl = round(price + prem, 2)
        out.append({
            'code': str(r.get('bond_id', '')), 'name': str(r.get('bond_nm', '')),
            'price': price, 'change_pct': _num(r.get('increase_rt')),
            'premium_pct': prem, 'conv_value': _num(r.get('convert_value')),
            'double_low': dl, 'rating': str(r.get('rating_cd', '') or ''),
            'stock_code': str(r.get('stock_id', '')), 'stock_name': str(r.get('stock_nm', '')),
            'ytm_pct': _num(r.get('ytm_rt')), 'remain_years': _num(r.get('year_left')),
            'remain_scale_yi': _num(r.get('curr_iss_amt')), 'turnover_pct': _num(r.get('turnover_rt')),
        })
    return out


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.jsl 直连自检 ===')
    cb = convertible_bonds()
    print(f'convertible_bonds: {len(cb)} 只(匿名约30)')
    if cb:
        print('  首只:', cb[0])
    print('OK' if cb else '⚠️ 空(可能被限/网络)')
