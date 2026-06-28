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


# 2026-06-27 阶段3:东财数据中心查询已归位 data/sources/eastmoney.datacenter;此处再导出,
# 本模块 _dragon_tiger_board 及外部 `from a_stock_data_adapter import _eastmoney_datacenter` 零改。
from data.sources.eastmoney import datacenter as _eastmoney_datacenter   # noqa: E402


# ============================================================
# Layer 1: 行情层
# ============================================================

# 2026-06-28 阶段3:批量行情三源(腾讯 / 东财 ulist / 新浪)已归位 data/sources/{tencent,eastmoney,sina}.py。
# _batch_quote(腾讯主 + 东财补缺)留本模块编排,用下列再导出名;类方法 / datahub 调用零改。
from data.sources.tencent import quotes as _tencent_quote                  # noqa: E402
from data.sources.eastmoney import ulist_quote as _eastmoney_ulist_quote   # noqa: E402
from data.sources.sina import quotes as _sina_quote                        # noqa: E402


# _eastmoney_ulist_quote 已归位 sources/eastmoney.ulist_quote(见上方阶段3 再导出块)。


# _sina_quote 已归位 sources/sina.quotes(见上方阶段3 再导出块)。


def _batch_quote(codes: list[str]) -> dict[str, dict]:
    """批量实时行情:腾讯主源,缺失的代码用东财 ulist 兜底(跨源容灾)。"""
    out = _tencent_quote(codes)
    missing = [c for c in codes if _normalize_code(c) not in out]
    if missing:
        out.update(_eastmoney_ulist_quote(missing))
    return out


# 2026-06-27 阶段3:百度股市通(K线/概念块)已归位 data/sources/baidu.py(再导出,调用零改)。
from data.sources.baidu import (   # noqa: E402
    kline_with_ma as _baidu_kline_with_ma,
    concept_blocks as _baidu_concept_blocks,
)


# ============================================================
# Layer 2: 研报层
# ============================================================

# 2026-06-27 阶段3:东财 研报/个股新闻/基本面 已归位 data/sources/eastmoney.py(再导出,调用零改)。
from data.sources.eastmoney import (   # noqa: E402
    reports as _eastmoney_reports,
    industry_reports as _eastmoney_industry_reports,
    stock_news as _eastmoney_stock_news,
    stock_info as _eastmoney_stock_info,
)


# 2026-06-27 阶段3:同花顺 一致预期/热点归因 已归位 data/sources/ths.py(再导出,调用零改)。
from data.sources.ths import (   # noqa: E402
    eps_forecast as _ths_eps_forecast,
    hot_reason as _ths_hot_reason,
)


# ============================================================
# Layer 3: 信号层
# ============================================================

# _ths_hot_reason 已归位 sources/ths(见上方阶段3 再导出块)。


# 2026-06-27 阶段3:东财 push2 资金流(个股分钟/日级 + 板块行业/概念)已归位 data/sources/eastmoney.py。
# 保留原私有函数名作再导出,类方法/datahub 调用零改。
from data.sources.eastmoney import (   # noqa: E402
    fund_flow_minute as _eastmoney_fund_flow_minute,
    fund_flow_history as _stock_fund_flow_120d,
    sector_fund_flow as _sector_fund_flow_push2,
    sector_fund_flow_bkzj as _sector_fund_flow_bkzj,
)


# _baidu_concept_blocks 已归位 sources/baidu(见上方阶段3 再导出块)。


# 2026-06-27 阶段3:行业/概念板块涨跌排名(east push2 clist)已归位 data/sources/eastmoney.py(再导出)。
from data.sources.eastmoney import (   # noqa: E402
    industry_comparison as _industry_comparison,
    concept_comparison as _concept_comparison,
)


# _concept_comparison 已归位 sources/eastmoney(见上方阶段3 再导出块)。


# _sector_fund_flow_push2 / _sector_fund_flow_bkzj 已归位 sources/eastmoney(见上方阶段3 再导出块)。


def _f_num(v) -> float:
    """容错转 float(板块兜底源用):'--'/空/None → 0.0。"""
    try:
        return float(str(v).replace('%', '').replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


def _sector_ranking_ths(sector_type: str = "industry", top_n: int = 20) -> dict:
    """行业/概念板块涨跌排名 —— 同花顺(akshare ths, data.10jqka.com.cn, **真非东财**)。

    作为东财 push2 `_industry_comparison`/`_concept_comparison` 的**真跨公司**兜底:
    东财机房 IP 被封时, 同花顺仍可取板块涨跌榜。产出与东财源逐字段同构
    {top,bottom,total}, 每条 {rank,name,change_pct,code,up_count,down_count,leader,leader_change}。
    ⚠️ 用纯 JSON 的 stock_fund_flow_industry/concept(自带涨跌幅+领涨股)按涨跌幅排序, **不用**
    stock_board_industry_summary_ths —— 后者底层 read_html 抓页面表格, 偶发 'No tables found' 不稳,
    兜底源求稳优先。代价:该接口无上涨/下跌家数 → up_count/down_count 补 0;无板块代码 → code 空。
    """
    import akshare as ak
    from data.akshare_safe import call as ak_call
    fn = ak.stock_fund_flow_concept if sector_type == "concept" else ak.stock_fund_flow_industry
    try:
        df = ak_call(fn, symbol="即时", timeout=20)
    except Exception as e:
        print(f"[a-stock] 板块排名(同花顺兜底)请求失败({sector_type}): {e}")
        return {"top": [], "bottom": [], "total": 0}
    if df is None or getattr(df, 'empty', True) or "行业" not in df.columns or "行业-涨跌幅" not in df.columns:
        return {"top": [], "bottom": [], "total": 0}
    df = df.sort_values("行业-涨跌幅", ascending=False)
    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        rows.append({
            "rank": i + 1,
            "name": r.get("行业", ""),
            "change_pct": _f_num(r.get("行业-涨跌幅")),
            "code": "",
            "up_count": 0,
            "down_count": 0,
            "leader": r.get("领涨股", "") or "",
            "leader_change": _f_num(r.get("领涨股-涨跌幅")),
        })
    return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}


def _sector_fund_flow_ths(sector_type: str = "industry", top_n: int = 50) -> list[dict]:
    """行业/概念板块资金流 —— 同花顺(akshare ths, data.10jqka.com.cn, **真非东财**)。

    作为东财 push2/bkzj 的**真跨公司**兜底(那两个都是东财, IP 被封时同死)。
    产出与 `_sector_fund_flow_push2` 逐字段同构 list[dict](按主力净额降序)。
    ⚠️ 单位:同花顺"净额"为**亿元**, 本项目板块资金流口径为**元**(见 _sector_fund_flow_push2)→ ×1e8 对齐;
    同花顺只给主力净额, 不分超大/大/中/小档 → 分档补 0;无板块代码 → code 空。
    """
    import akshare as ak
    from data.akshare_safe import call as ak_call
    fn = ak.stock_fund_flow_concept if sector_type == "concept" else ak.stock_fund_flow_industry
    try:
        df = ak_call(fn, symbol="即时", timeout=20)
    except Exception as e:
        print(f"[a-stock] 板块资金流(同花顺兜底)请求失败({sector_type}): {e}")
        return []
    need = ("行业", "行业-涨跌幅", "净额")
    if df is None or getattr(df, 'empty', True) or not all(c in df.columns for c in need):
        return []
    df = df.sort_values("净额", ascending=False)
    rows = []
    for _, r in df.head(top_n).iterrows():
        rows.append({
            "name": r.get("行业", ""),
            "code": "",
            "change_pct": _f_num(r.get("行业-涨跌幅")),
            "main_net_inflow": _f_num(r.get("净额")) * 1e8,   # 亿元 → 元(对齐 push2 口径)
            "main_net_inflow_pct": 0,
            "super_large_net_inflow": 0, "large_net_inflow": 0,
            "medium_net_inflow": 0, "small_net_inflow": 0,
            "leader": r.get("领涨股", "") or "",
            "leader_change": _f_num(r.get("领涨股-涨跌幅")),
        })
    return rows


# 2026-06-28 阶段3:个股龙虎榜聚合已归位 data/sources/eastmoney.py(并修空 records 的 NameError 隐患)。
from data.sources.eastmoney import dragon_tiger_board as _dragon_tiger_board   # noqa: E402


# ============================================================
# Layer 4: 资金面 / 筹码层
# ============================================================

# 2026-06-27 阶段3:个股公司数据(融资融券/大宗/股东户数/分红/解禁)已归位 data/sources/eastmoney.py。
# 这里保留原私有函数名作再导出,类方法与外部 `from a_stock_data_adapter import _xxx` 零改。
from data.sources.eastmoney import (   # noqa: E402
    margin as _margin_trading,
    block_trade as _block_trade,
    holder_num_change as _holder_num_change,
    dividend as _dividend_history,
    lockup_expiry as _lockup_expiry,
)


# ============================================================
# Layer 5: 新闻层
# ============================================================

# _eastmoney_stock_news 已归位 sources/eastmoney(见上方阶段3 再导出块)。


# 2026-06-27 阶段3:财联社电报已归位 data/sources/cls.py(再导出,调用零改)。
from data.sources.cls import telegraph as _cls_telegraph   # noqa: E402


# ============================================================
# Layer 6: 基础数据
# ============================================================

# _eastmoney_stock_info 已归位 sources/eastmoney(见上方阶段3 再导出块)。


# _sina_financial_report(新浪财报三表)已删(2026-06-28 阶段4):datahub.financials 改走
# sources/sina.financials(读对 report_list 层级出真实科目);本 adapter orphan 读错层级
# (result[report_type] 恒空)且已无调用方,连同 adapter.get_financial_reports /
# manager.get_financial_reports_a_stock 一并删除。
# _lockup_expiry 已归位 data/sources/eastmoney.lockup_expiry(见上方阶段3 再导出块)。


# ============================================================
# 公告层
# ============================================================

# 2026-06-27 阶段3:巨潮公告已归位 data/sources/cninfo.py(再导出,调用零改)。
from data.sources.cninfo import announcements as _cninfo_announcements   # noqa: E402


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

    def get_quotes_eastmoney(self, symbols: list[str]) -> dict[str, dict]:
        """纯东财 ulist 批量行情(与腾讯同构 dict)——供 datahub 作并列独立兜底源,
        腾讯整体卡死/被砍时由 datahub._route 独立超时切到这里。"""
        return _eastmoney_ulist_quote(symbols)

    def get_quotes_sina(self, symbols: list[str]) -> dict[str, dict]:
        """纯新浪 hq 批量行情(真·独立源,与腾讯同 key 集但无 PE/PB/市值)——供 datahub 作
        quotes 第三兜底:腾讯+东财(同公司)都挂时,新浪是唯一的非东财非腾讯独立源。"""
        return _sina_quote(symbols)

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

    def get_industry_reports(self, industry_code: str = "*", max_pages: int = 5,
                             begin: str = "2024-01-01") -> list[dict]:
        """获取东财行业研报列表(industry_code='*' 全行业)"""
        return _eastmoney_industry_reports(industry_code, max_pages, begin)

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

    def get_sector_fund_flow_bkzj(self, sector_type: str = "industry", top_n: int = 50) -> list[dict]:
        """板块资金流 —— 非 push2 兜底源（东财 datacenter getbkzj，受限网络下 push2 被墙时用）"""
        return _sector_fund_flow_bkzj(sector_type, top_n)

    def get_sector_ranking_ths(self, sector_type: str = "industry", top_n: int = 20) -> dict:
        """板块涨跌排名 —— 同花顺真跨源兜底（data.10jqka.com.cn，东财被封时用）"""
        return _sector_ranking_ths(sector_type, top_n)

    def get_sector_fund_flow_ths(self, sector_type: str = "industry", top_n: int = 50) -> list[dict]:
        """板块资金流 —— 同花顺真跨源兜底（data.10jqka.com.cn，东财被封时用）"""
        return _sector_fund_flow_ths(sector_type, top_n)

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

    # get_financial_reports 已删(2026-06-28 阶段4):datahub.financials 走 sources/sina.financials,
    # 旧 adapter._sina_financial_report orphan 读错层级且无调用方,连同本方法删除。

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
