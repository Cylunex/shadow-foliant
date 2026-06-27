# -*- coding: utf-8 -*-
"""data.sources.tencent —— 腾讯财经直连原子源(阶段 3)。

直连腾讯行情接口(qt.gtimg.cn),**不碰 akshare**。契约见 data/sources/README.md。
腾讯只提供 quotes / indices 两类能力(本阶段先落地 indices;quotes 随 adapter 归位时再补)。

契约铁律:异常吞掉返空([])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
"""
from __future__ import annotations

import re
from typing import Dict, List

from . import _common as C


def quotes(codes: List[str]) -> Dict[str, dict]:
    """腾讯实时行情(qt.gtimg,GBK)→ {code6: {name,price,...,pe_ttm,pb,mcap_yi,...}}。空/异常 → {}。
    字段位与 adapter._tencent_quote 逐字段一致(vals[1..52])。已带前缀的代码原样查询。"""
    prefixed = []
    for c in codes:
        if re.match(r'^(sh|sz|bj)\d+$', str(c).lower()):
            prefixed.append(str(c).lower())
        else:
            cc = C.norm_code(c)
            prefixed.append(f"{C.a_prefix(cc)}{cc}")
    try:
        C.throttle('tencent')
        data = C.http_get_text("https://qt.gtimg.cn/q=" + ",".join(prefixed),
                               headers={"User-Agent": C.DESKTOP_UA}, timeout=6, encoding="gbk")
    except Exception as e:
        print(f"[sources.tencent] 腾讯行情请求失败: {type(e).__name__}")
        return {}
    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        result[key[2:]] = {
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

# 大盘指数:腾讯 qt.gtimg 代码(HK 用 r_hkHSI,与 A 股 s_ 前缀解析口径不同)。
_INDICES = [
    ("上证指数", "s_sh000001"), ("深证成指", "s_sz399001"), ("创业板指", "s_sz399006"),
    ("科创50", "s_sh000688"), ("沪深300", "s_sh000300"), ("恒生指数", "r_hkHSI"),
]


def indices() -> List[dict]:
    """主要大盘指数实时 → [{name, value, change_amt, change_pct}]。空/异常 → []。
    腾讯 qt.gtimg 行 `v_<sym>="...~字段~..."`:cur=v[3];A 股 amt=v[4]/pct=v[5];HK(r_)用前收算。"""
    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(t for _, t in _INDICES)
        txt = C.http_get_text(url, headers={"Referer": "https://finance.qq.com"},
                              timeout=8, encoding="gbk")
        raw = {line.split("=", 1)[0].replace("v_", "").strip(): line.split('"', 2)[1].split("~")
               for line in txt.splitlines() if line.startswith("v_") and '="' in line}
        out = []
        for name, tsym in _INDICES:
            v = raw.get(tsym)
            if not v or len(v) < 6:
                continue
            try:
                cur = float(v[3])
                if tsym.startswith("r_"):
                    prev = float(v[4])
                    amt = cur - prev
                    pct = (amt / prev * 100) if prev else 0
                else:
                    amt = float(v[4])
                    pct = float(v[5])
                out.append({"name": name, "value": cur, "change_amt": amt, "change_pct": pct})
            except Exception:
                continue
        return out
    except Exception:
        return []


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.tencent 直连自检 ===')
    idx = indices()
    print(f'indices: {len(idx)} 个;', idx[:3])
    print('OK' if idx else '⚠️ 空(可能网络/被封)')
