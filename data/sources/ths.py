# -*- coding: utf-8 -*-
"""data.sources.ths —— 同花顺(10jqka)直连原子源(阶段 3)。

直连同花顺公开接口(basic.10jqka / zx.10jqka),**不碰 akshare**。契约见 data/sources/README.md。
本阶段落地:eps_forecast(机构一致预期 EPS)、hot_reason(当日强势股归因 + 题材)。
(注:板块涨跌排名/资金流的同花顺兜底现走 akshare ths 接口,属末位兜底层,留待阶段4 sources/akshare.py。)

契约铁律:异常吞掉返空(空 DataFrame)、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
"""
from __future__ import annotations

from io import StringIO

import pandas as pd

from . import _common as C


def eps_forecast(code: str) -> pd.DataFrame:
    """同花顺机构一致预期 EPS(basic.10jqka worth.html 表格)→ DataFrame(原始列)或空 DF。"""
    c = C.norm_code(code)
    try:
        r = C.requests_session().get(
            f"https://basic.10jqka.com.cn/new/{c}/worth.html",
            headers={"User-Agent": C.DESKTOP_UA, "Referer": "https://basic.10jqka.com.cn/"},
            timeout=15)
        r.encoding = "gbk"
        # pandas 2.1+ 不接受裸 HTML 串(会当文件路径) → StringIO 包装。
        dfs = pd.read_html(StringIO(r.text))
        for df in dfs:
            cols = [str(x) for x in df.columns]
            if any("每股收益" in x or "均值" in x for x in cols):
                return df
        return dfs[0] if dfs else pd.DataFrame()
    except ValueError as e:
        # "No tables found" = 该股无机构一致预期(同花顺返空表),正常语义,静默返空避免刷屏。
        if 'No tables found' in str(e):
            return pd.DataFrame()
        print(f"[sources.ths] 一致预期({c}) 解析失败: {type(e).__name__}: {str(e)[:120]}")
        return pd.DataFrame()
    except Exception as e:
        print(f"[sources.ths] 一致预期({c}) 请求失败: {type(e).__name__}: {str(e)[:120]}")
        return pd.DataFrame()


def hot_reason(date: str = None) -> pd.DataFrame:
    """同花顺当日强势股归因 + 题材标签 → DataFrame(中文列)或空 DF。"""
    if date is None:
        from datetime import date as _date
        date = _date.today().strftime("%Y-%m-%d")
    url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
    try:
        data = C.requests_session().get(url, headers={"User-Agent": C.DESKTOP_UA}, timeout=10).json()
        if data.get("errocode", 0) != 0:
            raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")
        df = pd.DataFrame(data.get("data") or [])
        if df.empty:
            return df
        return df.rename(columns={
            "name": "名称", "code": "代码", "reason": "题材归因", "close": "收盘价",
            "zhangdie": "涨跌额", "zhangfu": "涨幅%", "huanshou": "换手率%",
            "chengjiaoe": "成交额", "chengjiaoliang": "成交量", "ddejingliang": "大单净量", "market": "市场",
        })
    except Exception as e:
        print(f"[sources.ths] 同花顺热点请求失败: {e}")
        return pd.DataFrame()


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.ths 直连自检 ===')
    e = eps_forecast('600519')
    print(f'eps_forecast 600519: {e.shape if hasattr(e, "shape") else None}')
    h = hot_reason()
    print(f'hot_reason today: {len(h)} 行; 列={list(h.columns)[:6] if not h.empty else None}')
    print('OK')
