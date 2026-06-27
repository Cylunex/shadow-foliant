# -*- coding: utf-8 -*-
"""data.sources.tencent —— 腾讯财经直连原子源(阶段 3)。

直连腾讯行情接口(qt.gtimg.cn),**不碰 akshare**。契约见 data/sources/README.md。
腾讯只提供 quotes / indices 两类能力(本阶段先落地 indices;quotes 随 adapter 归位时再补)。

契约铁律:异常吞掉返空([])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
"""
from __future__ import annotations

from typing import List

from . import _common as C

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
