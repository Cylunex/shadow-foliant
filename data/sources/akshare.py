# -*- coding: utf-8 -*-
"""data.sources.akshare —— akshare 末位兜底层(整合库,**仅此模块 + tushare.py 可 import akshare**)。

定位(见 docs/数据源原子化重构计划.md §2):akshare 是第三方「整合体」(内部偷偷包东财/新浪/
同花顺等真源,实际打哪不可控),故**绝不进主路径**,只作各域 `_route` 的**末位安全网**——
直连真源全挂时还能兜一手。本模块把散落在 datahub/adapter/manager 的 akshare 调用收口到一处,
逐域归一成项目契约后返回。

落地能力(随阶段3⑤/阶段4 增补):
  · kline(code, period, interval, adjust) —— stock_zh_a_hist(个股)/ fund_etf_hist_em(ETF),
        成交量「手」→「股」×100,归一 DatetimeIndex='Date' + 大写 OHLCV。
  · sector_ranking / sector_fund_flow(sector_type, top_n) —— 同花顺 ths 行业/概念 涨跌排名 + 资金流
        (东财 push2 的真非东财末位兜底);sector_spot() —— 同花顺行业板块快照。

契约铁律:异常吞掉返空(空 DataFrame / [])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
⚠️ akshare 走东财系接口,东财被封时与 east 同死(非真跨源),故排各域**末位**。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from . import _common as C

_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095, "5y": 1825}
# ETF / LOF 代码前缀(与 data_source_manager._is_etf_code 同口径):沪 51/56/58、深 15/16。
_ETF_PREFIX = ('51', '56', '58', '15', '16')


def _is_etf(code: str) -> bool:
    c = C.norm_code(code)
    return c.isdigit() and len(c) == 6 and c[:2] in _ETF_PREFIX


def _date_range(period: str):
    """period → (start, end) 'YYYYMMDD'。≤1y 兜底取 ~520 自然日(≈365 交易日)对齐链内主源深度。"""
    days = max(_PERIOD_DAYS.get(period, 365), 520) if period in ('1mo', '3mo', '6mo', '1y') \
        else _PERIOD_DAYS.get(period, 365) + 40
    end = datetime.now()
    return (end - timedelta(days=days)).strftime('%Y%m%d'), end.strftime('%Y%m%d')


def kline(code: str, period: str = "1y", interval: str = "1d", adjust: str = "raw") -> pd.DataFrame:
    """akshare 日线(末位兜底)。adjust='raw' 不复权 / 'qfq' 前复权。仅日线。
    个股走 stock_zh_a_hist、ETF 走 fund_etf_hist_em;成交量「手」→「股」×100。
    返回项目契约 DatetimeIndex='Date' + 大写 OHLCV,或空 DF。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    code6 = C.norm_code(code)
    ak_adjust = 'qfq' if str(adjust) == 'qfq' else ''   # ''=不复权(raw)
    start, end = _date_range(period)
    try:
        import akshare as ak
    except Exception:
        return pd.DataFrame()
    try:
        C.throttle('akshare')
        if _is_etf(code6):
            df = C.ak_safe(ak.fund_etf_hist_em, timeout=30, symbol=code6, period='daily',
                           start_date=start, end_date=end, adjust=ak_adjust)
        else:
            df = C.ak_safe(ak.stock_zh_a_hist, timeout=30, symbol=code6, period='daily',
                           start_date=start, end_date=end, adjust=ak_adjust)
    except Exception:
        return pd.DataFrame()
    if df is None or getattr(df, 'empty', True):
        return pd.DataFrame()
    # 东财系 成交量单位「手」→「股」×100(对齐 sina/east/tencent 的「股」口径)。
    return C.to_ohlcv(df, date_col='日期', vol_mult=100.0)


# ── 板块兜底(同花顺 ths,真非东财:东财 push2 被封时兜底)─────────────────────────
def _num(v) -> float:
    """容错转 float(板块兜底源用):'--'/空/None → 0.0。"""
    try:
        return float(str(v).replace('%', '').replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


def sector_ranking(sector_type: str = "industry", top_n: int = 20) -> dict:
    """行业/概念板块涨跌排名(同花顺 stock_fund_flow_industry/concept,真非东财)——东财 push2 排名末位兜底。
    产出与东财源逐字段同构 {top,bottom,total};该接口无上涨/下跌家数 → up/down_count 补 0、无板块代码 → code 空。"""
    try:
        import akshare as ak
    except Exception:
        return {"top": [], "bottom": [], "total": 0}
    fn = ak.stock_fund_flow_concept if sector_type == "concept" else ak.stock_fund_flow_industry
    try:
        C.throttle('akshare')
        df = C.ak_safe(fn, symbol="即时", timeout=20)
    except Exception as e:
        print(f"[sources.akshare] 板块排名(同花顺兜底)请求失败({sector_type}): {e}")
        return {"top": [], "bottom": [], "total": 0}
    if df is None or getattr(df, 'empty', True) or "行业" not in df.columns or "行业-涨跌幅" not in df.columns:
        return {"top": [], "bottom": [], "total": 0}
    df = df.sort_values("行业-涨跌幅", ascending=False)
    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        rows.append({
            "rank": i + 1, "name": r.get("行业", ""),
            "change_pct": _num(r.get("行业-涨跌幅")), "code": "",
            "up_count": 0, "down_count": 0,
            "leader": r.get("领涨股", "") or "", "leader_change": _num(r.get("领涨股-涨跌幅")),
        })
    return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}


def sector_fund_flow(sector_type: str = "industry", top_n: int = 50) -> list:
    """行业/概念板块资金流(同花顺,真非东财)——东财 push2/bkzj 末位兜底。逐字段同构 list[dict](主力净额降序)。
    单位:同花顺「净额」为**亿元**,本项目板块资金流口径为**元** → ×1e8 对齐;只给主力净额 → 分档补 0。"""
    try:
        import akshare as ak
    except Exception:
        return []
    fn = ak.stock_fund_flow_concept if sector_type == "concept" else ak.stock_fund_flow_industry
    try:
        C.throttle('akshare')
        df = C.ak_safe(fn, symbol="即时", timeout=20)
    except Exception as e:
        print(f"[sources.akshare] 板块资金流(同花顺兜底)请求失败({sector_type}): {e}")
        return []
    need = ("行业", "行业-涨跌幅", "净额")
    if df is None or getattr(df, 'empty', True) or not all(c in df.columns for c in need):
        return []
    df = df.sort_values("净额", ascending=False)
    rows = []
    for _, r in df.head(top_n).iterrows():
        rows.append({
            "name": r.get("行业", ""), "code": "",
            "change_pct": _num(r.get("行业-涨跌幅")),
            "main_net_inflow": _num(r.get("净额")) * 1e8,   # 亿元 → 元(对齐 push2 口径)
            "main_net_inflow_pct": 0,
            "super_large_net_inflow": 0, "large_net_inflow": 0,
            "medium_net_inflow": 0, "small_net_inflow": 0,
            "leader": r.get("领涨股", "") or "", "leader_change": _num(r.get("领涨股-涨跌幅")),
        })
    return rows


def sector_spot() -> list:
    """行业板块快照(同花顺 stock_board_industry_summary_ths,真非东财)→ [{板块,涨跌幅,领涨}](涨幅降序)。
    列对不齐/空/异常 → []。⚠️ 该接口底层 read_html 抓页面表,偶发 'No tables found' 不稳 → 仅作末位兜底。"""
    try:
        import akshare as ak
    except Exception:
        return []
    try:
        C.throttle('akshare')
        df = C.ak_safe(ak.stock_board_industry_summary_ths, timeout=20)
    except Exception:
        return []
    if df is None or getattr(df, 'empty', True) or '板块' not in df.columns or '涨跌幅' not in df.columns:
        return []
    rows = [{"板块": r.get("板块"), "涨跌幅": round(float(r.get("涨跌幅") or 0), 2),
             "领涨": r.get("领涨股") or ""} for _, r in df.iterrows()]
    return sorted(rows, key=lambda x: x["涨跌幅"], reverse=True)


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.akshare 末位兜底自检 ===')
    for c in ('600519', '159915'):
        for adj in ('raw', 'qfq'):
            d = kline(c, '3mo', adjust=adj)
            print(f'  {c} {adj}: {len(d)} bars', '' if d.empty else f"last={d.index[-1].date()} C={d['Close'].iloc[-1]} V={d['Volume'].iloc[-1]:.0f}")
    rk = sector_ranking('industry', 20)
    print(f"  sector_ranking industry: total={rk.get('total')} top1={(rk.get('top') or [{}])[0].get('name')}")
    ff = sector_fund_flow('industry', 50)
    print(f"  sector_fund_flow industry: {len(ff)} 行 top1={(ff or [{}])[0].get('name')}")
    ss = sector_spot()
    print(f"  sector_spot: {len(ss)} 板块 top1={(ss or [{}])[0].get('板块')}")
    print('OK')
