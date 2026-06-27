# -*- coding: utf-8 -*-
"""data.sources.baidu —— 百度股市通(gushitong)直连原子源(阶段 3)。

直连 finance.pae.baidu.com,**不碰 akshare**。契约见 data/sources/README.md。
能力:kline_with_ma(自带 MA5/10/20 的 K线)、concept_blocks(概念/行业/地域归属)。
"""
from __future__ import annotations

from typing import List  # noqa: F401

from . import _common as C

_HEADERS = {
    "User-Agent": C.DESKTOP_UA,
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


def _empty_blocks() -> dict:
    return {"industry": [], "concept": [], "region": [], "concept_tags": []}


def kline_with_ma(code: str, start_time: str = "") -> dict:
    """百度股市通 K线(自带 MA5/10/20)→ {keys:[...], rows:[...]}。空/异常 → {keys:[],rows:[]}。"""
    params = {"all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
              "isFutures": "false", "isStock": "true", "newFormat": "1",
              "group": "quotation_kline_ab", "finClientType": "pc",
              "code": C.norm_code(code), "start_time": start_time, "ktype": "1"}
    try:
        d = C.requests_session().get("https://finance.pae.baidu.com/selfselect/getstockquotation",
                                     params=params, headers=_HEADERS, timeout=10).json()
        md = ((d.get("Result", {}) or {}).get("newMarketData", {})) or {}
        return {"keys": md.get("keys", []), "rows": md.get("marketData", "").split(";")}
    except Exception as e:
        print(f"[sources.baidu] 百度K线请求失败: {e}")
        return {"keys": [], "rows": []}


def concept_blocks(code: str) -> dict:
    """百度股市通概念/行业/地域归属 → {industry,concept,region,concept_tags}。空/异常 → 同结构空。"""
    c = C.norm_code(code)
    url = f"https://finance.pae.baidu.com/api/getrelatedblock?code={c}&market=ab&typeCode=all&finClientType=pc"
    try:
        d = C.requests_session().get(url, headers=_HEADERS, timeout=10).json()
        if str(d.get("ResultCode", -1)) != "0":
            return _empty_blocks()
        result = _empty_blocks()
        for block in d.get("Result", []):
            bt = block.get("type", "")
            for item in block.get("list", []):
                entry = {"name": item.get("name", ""), "change_pct": item.get("increase", ""),
                         "desc": item.get("desc", "")}
                if "行业" in bt:
                    result["industry"].append(entry)
                elif "概念" in bt:
                    result["concept"].append(entry)
                    result["concept_tags"].append(entry["name"])
                elif "地域" in bt:
                    result["region"].append(entry)
        return result
    except Exception as e:
        print(f"[sources.baidu] 概念板块请求失败: {e}")
        return _empty_blocks()


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.baidu 直连自检 ===')
    k = kline_with_ma('600519')
    print(f'kline_with_ma 600519: keys={len(k["keys"])} rows={len(k["rows"])}')
    b = concept_blocks('600519')
    print(f'concept_blocks 600519: industry={len(b["industry"])} concept={len(b["concept"])} region={len(b["region"])}')
    print('OK')
