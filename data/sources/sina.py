# -*- coding: utf-8 -*-
"""data.sources.sina —— 新浪财经直连原子源(阶段 2)。

直连本家 provider(finance.sina.com.cn / quotes.sina.cn),**不碰 akshare / py_mini_racer**。
归一工具一律从 data/sources/_common.py 取;契约见 data/sources/README.md。

落地能力:
  · kline(code, period, interval, adjust)
      raw  —— getKLineData 清洁 JSON(不复权,volume 本就「股」)。
      qfq  —— raw ÷ qfq.js 复权因子(round 2),与 akshare stock_zh_a_daily(adjust='qfq') 同口径,
              但用清洁 JSON 取 raw + 直接读因子,**免 py_mini_racer JS 解密**。
              因子稀疏(只在除权日变),用 merge_asof(backward) 取「最近一个 ≤ 当日的因子」
              —— 等价 akshare 的 outer-merge + ffill,但不会凭空造 bar。
  · sector_spot()  —— 新浪行业板块快照(newSinaHy.php,GBK)。
  · financials(code, report_type) —— 新浪财报三表(openapi getFinanceReport2022)。

契约铁律:异常吞掉返空(空 DataFrame / [])、不读缓存、不做跨源降级 —— 那是 datahub._route 的事。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

import pandas as pd

from . import _common as C


def quotes(codes: List[str]) -> Dict[str, dict]:
    """新浪实时行情(hq.sinajs,GBK,需 Referer)→ {code6: {name,price,...}}。空/异常 → {}。
    新浪只有行情(无 PE/PB/市值/换手/涨跌停 → 0),key 集与腾讯源一致;与 adapter._sina_quote 逐字段一致。"""
    prefixed = []
    for c in codes:
        if re.match(r'^(sh|sz|bj)\d+$', str(c).lower()):
            prefixed.append(str(c).lower())
        else:
            cc = C.norm_code(c)
            prefixed.append(f"{C.a_prefix(cc)}{cc}")
    try:
        C.throttle('sina')
        raw = C.http_get_text("https://hq.sinajs.cn/list=" + ",".join(prefixed),
                              headers={"User-Agent": C.DESKTOP_UA, "Referer": "https://finance.sina.com.cn"},
                              timeout=6, encoding="gbk")
    except Exception as e:
        print(f"[sources.sina] 新浪行情请求失败: {type(e).__name__}")
        return {}
    result = {}
    for line in raw.strip().split("\n"):
        m = re.search(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', line)
        if not m:
            continue
        f = m.group(2).split(",")          # 名称,今开,昨收,现价,最高,最低,买一,卖一,量(股),额(元),...
        if len(f) < 10 or not f[0]:
            continue
        try:
            open_ = float(f[1]); last = float(f[2]); price = float(f[3])
            high = float(f[4]); low = float(f[5]); amount = float(f[9])
        except (ValueError, IndexError):
            continue
        if price <= 0:                     # 停牌/集合竞价前现价 0 → 用昨收兜
            price = last
        chg = price - last
        pct = (chg / last * 100) if last else 0.0
        result[m.group(1)[2:]] = {
            "name": f[0], "price": price, "last_close": last, "open": open_,
            "change_amt": round(chg, 2), "change_pct": round(pct, 2),
            "high": high, "low": low, "amount_wan": amount / 1e4,
            "turnover_pct": 0.0, "pe_ttm": 0.0,
            "amplitude_pct": round((high - low) / last * 100, 2) if last else 0.0,
            "mcap_yi": 0.0, "float_mcap_yi": 0.0, "pb": 0.0,
            "limit_up": 0.0, "limit_down": 0.0, "vol_ratio": 0.0, "pe_static": 0.0,
        }
    return result


# ── K线 ────────────────────────────────────────────────────────────────────
# 清洁 JSON 日线(不复权,money.finance.sina getKLineData)。volume 单位「股」。
_KLINE_URL = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
# 前复权因子(稀疏,只在除权除息日变;最近日因子=1.0,历史>1.0)。返回 `var Xqfq={...};/*注释*/`。
_QFQ_URL = "https://finance.sina.com.cn/realstock/company/{sym}/qfq.js"

_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095, "5y": 1825}


def _datalen(period: str) -> int:
    """period → getKLineData datalen(交易日 ≈ 自然日 ×0.72,加 30 缓冲;封顶 1100)。
    ⚠️ ≤1y 兜底 365 根:对齐旧 fetcher/manager 新浪路径(datalen=365 定值、raw 不按 period 截),
    使摊平后 raw 行数与改前一致(下游 position_guardian/InStock 策略按既有行数预期)。
    qfq 侧由 datahub._kline_sina_qfq 再 _slice_by_days 截到 period,故本兜底不改 qfq 行数。"""
    days = _PERIOD_DAYS.get(period, 365)
    return min(max(int(days * 0.72) + 30, 365), 1100)


def _raw_daily(code: str, period: str) -> pd.DataFrame:
    """新浪 getKLineData 不复权日线 → 归一 OHLCV(DatetimeIndex='Date',volume「股」)。空→空 DF。"""
    n = _datalen(period)
    C.throttle('sina')
    arr = C.http_get_json(_KLINE_URL.format(sym=C.sina_code(code), n=n), timeout=12)
    if not isinstance(arr, list) or not arr:
        return pd.DataFrame()
    # getKLineData volume 本就是「股」(非「手」),不乘 100。
    return C.to_ohlcv(pd.DataFrame(arr), date_col='day', vol_mult=1.0)


def _qfq_factor(code: str) -> pd.DataFrame:
    """qfq.js → DataFrame[Date, qfq_factor](升序)。无因子/异常 → 空 DF。"""
    C.throttle('sina')
    txt = C.http_get_text(_QFQ_URL.format(sym=C.sina_code(code)), timeout=12)
    i = txt.find('{')
    if i < 0:
        return pd.DataFrame()
    obj, _end = json.JSONDecoder().raw_decode(txt[i:])   # 只解第一个 JSON,忽略尾部反爬注释
    data = (obj or {}).get('data') or []
    if not data:
        return pd.DataFrame()
    fdf = pd.DataFrame(data)            # 列: d(日期), f(因子)
    if not {'d', 'f'}.issubset(fdf.columns):
        return pd.DataFrame()
    fdf = pd.DataFrame({
        'Date': pd.to_datetime(fdf['d'], errors='coerce').dt.normalize(),
        'qfq_factor': pd.to_numeric(fdf['f'], errors='coerce'),
    }).dropna().sort_values('Date')
    return fdf


def kline(code: str, period: str = "1y", interval: str = "1d", adjust: str = "qfq") -> pd.DataFrame:
    """新浪日线。adjust='raw' 不复权 / 'qfq' 前复权。仅日线(其它 interval 返空)。
    返回项目契约:DatetimeIndex='Date' + 大写 Open/High/Low/Close/Volume(volume「股」),或空 DF。"""
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()
    try:
        raw = _raw_daily(code, period)
        if raw.empty:
            return pd.DataFrame()
        if str(adjust) != 'qfq':
            return raw
        fdf = _qfq_factor(code)
        if fdf.empty:
            return pd.DataFrame()   # 取不到因子 → 让 _route 切下一个 qfq 源(别拿 raw 冒充 qfq)
        r = raw.reset_index().sort_values('Date')
        m = pd.merge_asof(r, fdf, on='Date', direction='backward')   # 每日取「最近 ≤ 当日」的因子
        m = m.set_index('Date')
        for c in ('Open', 'High', 'Low', 'Close'):
            m[c] = (m[c] / m['qfq_factor']).round(2)   # 前复权:除以因子(与 akshare 同口径)
        # Volume 不复权(与 akshare qfq 一致);丢掉无因子的早期行
        m = m.dropna(subset=['Close', 'qfq_factor'])
        return m[['Open', 'High', 'Low', 'Close', 'Volume']]
    except Exception:
        return pd.DataFrame()


# ── 行业板块快照 ─────────────────────────────────────────────────────────────
_SECTOR_URL = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"


def sector_spot() -> List[dict]:
    """新浪行业板块快照 → [{板块, 涨跌幅, 领涨}](涨幅降序)。空/异常 → []。
    源格式:`var ...={"key":"label,板块,公司家数,平均价,涨跌额,涨跌幅,量,额,代码,涨跌幅,价,涨跌额,领涨股名"}`。"""
    try:
        C.throttle('sina')
        txt = C.http_get_text(_SECTOR_URL, timeout=12, encoding='gbk')
        i = txt.find('{')
        if i < 0:
            return []
        obj, _end = json.JSONDecoder().raw_decode(txt[i:])
        rows = []
        for v in (obj or {}).values():
            parts = str(v).split(',')
            if len(parts) < 13:
                continue
            try:
                chg = round(float(parts[5]), 2)
            except Exception:
                continue
            rows.append({"板块": parts[1], "涨跌幅": chg, "领涨": parts[12]})
        rows.sort(key=lambda x: x["涨跌幅"], reverse=True)
        return rows
    except Exception:
        return []


# ── 大盘指数 ────────────────────────────────────────────────────────────────
# 新浪 hq.sinajs 代码(HK 用 rt_hkHSI,字段位与 A 股不同)。
_INDICES = [
    ("上证指数", "s_sh000001"), ("深证成指", "s_sz399001"), ("创业板指", "s_sz399006"),
    ("科创50", "s_sh000688"), ("沪深300", "s_sh000300"), ("恒生指数", "rt_hkHSI"),
]


def indices() -> List[dict]:
    """主要大盘指数实时 → [{name, value, change_amt, change_pct}]。空/异常 → []。
    新浪 hq.sinajs 行 `var hq_str_<sym>="逗号字段"`:A 股 value=v[1]/amt=v[2]/pct=v[3];HK(rt_hk)用 v[6..8]。
    ⚠️ hq.sinajs 需带 Referer(防盗链),gb2312 编码。"""
    try:
        url = "https://hq.sinajs.cn/list=" + ",".join(s for _, s in _INDICES)
        txt = C.http_get_text(url, headers={"Referer": "https://finance.sina.com.cn"},
                              timeout=8, encoding="gb2312")
        raw = {line.split("=", 1)[0].replace("var", "").strip()[7:]: line.split('"', 2)[1].split(",")
               for line in txt.splitlines() if "hq_str_" in line and '="' in line}
        out = []
        for name, ssym in _INDICES:
            v = raw.get(ssym)
            if not v:
                continue
            try:
                if ssym.startswith("rt_hk"):
                    out.append({"name": name, "value": float(v[6]),
                                "change_amt": float(v[7]), "change_pct": float(v[8])})
                else:
                    out.append({"name": name, "value": float(v[1]),
                                "change_amt": float(v[2]), "change_pct": float(v[3])})
            except Exception:
                continue
        return out
    except Exception:
        return []


# ── 财报三表 ────────────────────────────────────────────────────────────────
_FIN_URL = ("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
            "?paperCode={sym}&source={src}&type=0&page=1&num={num}")


def financials(code: str, report_type: str = "lrb", num: int = 20) -> List[dict]:
    """新浪财报三表。report_type: lrb 利润 / fzb 资产负债 / llb 现金流。
    返回 list[dict]:一期一条(新→旧),含 {报告期, 报告类型, 币种, 公告日期} + 各科目(item_title→数值)。
    空/异常 → []。"""
    rt = report_type if report_type in ('lrb', 'fzb', 'llb') else 'lrb'
    try:
        C.throttle('sina')
        d = C.http_get_json(_FIN_URL.format(sym=C.sina_code(code), src=rt, num=num), timeout=15)
    except Exception:
        return []
    try:
        rl = ((((d or {}).get('result') or {}).get('data') or {}).get('report_list')) or {}
        out = []
        for date_key in sorted(rl.keys(), reverse=True):     # 报告期新→旧
            node = rl[date_key] or {}
            rec = {"报告期": date_key, "报告类型": node.get('rType'),
                   "币种": node.get('rCurrency'), "公告日期": node.get('publish_date')}
            for it in (node.get('data') or []):
                title = it.get('item_title')
                if not title:
                    continue
                try:
                    rec[title] = float(it.get('item_value'))
                except Exception:
                    rec[title] = it.get('item_value')
            out.append(rec)
        return out
    except Exception:
        return []


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.sina 直连自检 ===')
    q = kline('600519', '6mo', adjust='qfq')
    print(f'qfq kline 600519 6mo: {len(q)} bars')
    if not q.empty:
        print(q.tail(2).to_string())
    r = kline('600519', '3mo', adjust='raw')
    print(f'raw kline 600519 3mo: {len(r)} bars')
    s = sector_spot()
    print(f'sector_spot: {len(s)} 行业;', s[:2])
    f = financials('600519', 'lrb')
    print(f'financials lrb: {len(f)} 期;', (list(f[0].keys())[:6] if f else None))
    print('OK' if (not q.empty and r.shape[0] and s and f) else '⚠️ 部分能力空(可能网络/被封)')
