"""
增强选股模块 — 基于东财选股器 + InStock 选股逻辑

支持:
  - 东财 200+ 维度综合选股
  - 组合条件筛选（基本面+技术面+消息面+行情）
  - 结果排序和格式化

用法:
    from stock_selection import StockSelector
    selector = StockSelector()
    # 获取全市场股票列表（含基本面数据）
    stocks = selector.get_stock_pool()
    # 带条件筛选
    picks = selector.screen(market='主板', pe_max=30, ma_cross='golden')
"""

import math
import time
import random
import requests
import pandas as pd
from datetime import datetime
from typing import Optional

# ─── 东财选股器字段映射 ───────────────────────────────────

STOCK_SELECTION_COLUMNS = {
    # 基本信息
    "SECUCODE": {"map": "SECUCODE", "name": "全代码"},
    "SECURITY_CODE": {"map": "SECURITY_CODE", "name": "代码"},
    "SECURITY_NAME_ABBR": {"map": "SECURITY_NAME_ABBR", "name": "名称"},
    "MARKET": {"map": "MARKET", "name": "市场"},
    # 行情
    "NEW_PRICE": {"map": "NEW_PRICE", "name": "最新价"},
    "CHANGE_RATE": {"map": "CHANGE_RATE", "name": "涨跌幅"},
    "VOLUME_RATIO": {"map": "VOLUME_RATIO", "name": "量比"},
    "TURNOVERRATE": {"map": "TURNOVERRATE", "name": "换手率"},
    "AMOUNT": {"map": "AMOUNT", "name": "成交额"},
    # 估值
    "PE_TTM": {"map": "PE_TTM", "name": "PE(TTM)"},
    "PB": {"map": "PB", "name": "PB"},
    "TOTAL_MARKET_CAP": {"map": "TOTAL_MARKET_CAP", "name": "总市值"},
    "CIRCULATION_MARKET_CAP": {"map": "CIRCULATION_MARKET_CAP", "name": "流通市值"},
    # 财务
    "TOTAL_OPERATE_INCOME": {"map": "TOTAL_OPERATE_INCOME", "name": "营业收入"},
    "PARENT_NETPROFIT": {"map": "PARENT_NETPROFIT", "name": "净利润"},
    "WEIGHTAVG_ROE": {"map": "WEIGHTAVG_ROE", "name": "ROE"},
    "GROSSPROFIT_MARGIN": {"map": "GROSSPROFIT_MARGIN", "name": "毛利率"},
    "BASIC_EPS": {"map": "BASIC_EPS", "name": "EPS"},
    # 技术
    "MACD_GOLDEN_CROSS": {"map": "MACD_GOLDEN_CROSS", "name": "MACD金叉"},
    "KDJ_GOLDEN_CROSS": {"map": "KDJ_GOLDEN_CROSS", "name": "KDJ金叉"},
    "MA_GOLDEN_CROSS": {"map": "MA_GOLDEN_CROSS", "name": "均线金叉"},
    "VOLUME_BREAK": {"map": "VOLUME_BREAK", "name": "放量突破"},
    # 资金流向
    "MAIN_NET_INFLOW": {"map": "MAIN_NET_INFLOW", "name": "主力净流入"},
    "BIG_ORDER_NET_INFLOW": {"map": "BIG_ORDER_NET_INFLOW", "name": "大单净流入"},
}


class StockSelector:
    """A股综合选股器"""

    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.UA,
            'Referer': 'https://data.eastmoney.com/xuangu/',
        })

    def _safe_request(self, url, params, retries=3, timeout=15):
        """带重试的请求"""
        for i in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=timeout)
                r.raise_for_status()
                return r
            except Exception as e:
                if i < retries - 1:
                    time.sleep(random.uniform(1, 2))
                else:
                    print(f"[选股] 请求失败: {e}")
                    raise
        return None

    def get_stock_pool(self, markets: list = None,
                       filter_extra: str = "",
                       page_size: int = 50) -> pd.DataFrame:
        """
        从东财选股器获取全市场股票列表

        Args:
            markets: 市场列表，默认 ["上交所主板","深交所主板","深交所创业板"]
            filter_extra: 额外筛选条件（东财格式）
            page_size: 每页数量

        Returns:
            DataFrame with stock data
        """
        if markets is None:
            markets = ["上交所主板", "深交所主板", "深交所创业板"]

        # 构建字段
        sty_parts = []
        for k, v in STOCK_SELECTION_COLUMNS.items():
            sty_parts.append(v["map"])
        sty = ",".join(sty_parts)

        # 构建市场筛选
        market_conditions = "+".join(f'"{m}"' for m in markets)
        base_filter = f"(MARKET+in+({market_conditions}))(NEW_PRICE>0)"
        if filter_extra:
            base_filter = f"({base_filter})({filter_extra})"

        url = "https://data.eastmoney.com/dataapi/xuangu/list"
        params = {
            "sty": sty,
            "filter": base_filter,
            "p": 1,
            "ps": page_size,
            "source": "SELECT_SECURITIES",
            "client": "WEB",
        }

        try:
            r = self._safe_request(url, params)
            data = r.json()
            rows = data["result"]["data"]
            total = data["result"]["count"]
            total_pages = math.ceil(total / page_size)

            print(f"[选股] 共 {total} 只股票, {total_pages} 页")

            # 翻页获取
            for page in range(2, min(total_pages + 1, 31)):  # 最多30页=1500只
                time.sleep(random.uniform(0.5, 1))
                params["p"] = page
                r = self._safe_request(url, params)
                page_data = r.json()
                rows.extend(page_data["result"]["data"])

            df = pd.DataFrame(rows)
            # 重命名列为中文
            rename = {v["map"]: v["name"] for k, v in STOCK_SELECTION_COLUMNS.items()
                      if v["map"] in df.columns}
            df = df.rename(columns=rename)

            print(f"[选股] ✅ 获取 {len(df)} 只股票")
            return df

        except Exception as e:
            print(f"[选股] ❌ 获取股票池失败: {e}")
            return pd.DataFrame()

    def screen(self, df: pd.DataFrame = None, **conditions) -> pd.DataFrame:
        """
        条件筛选

        支持条件:
            market: 市场名称（如"上交所主板"）
            pe_min/pe_max: PE范围
            pb_min/pb_max: PB范围
            roe_min: ROE下限
            change_min/change_max: 涨跌幅范围
            turnover_min: 换手率下限
            mcap_min: 市值下限(亿)
            ma_cross: MACD金叉 'golden' / 'death' / 'any'
            vol_break: 放量突破 True
            sort_by: 排序字段
            ascending: 升序? (默认False=降序)
            top_n: 取前N只
        """
        if df is None:
            df = self.get_stock_pool()

        if df.empty:
            return df

        # 数值条件过滤
        for col, (cmin, cmax) in {
            "PE(TTM)": ("pe_min", "pe_max"),
            "PB": ("pb_min", "pb_max"),
            "涨跌幅": ("change_min", "change_max"),
        }.items():
            if col in df.columns:
                cmin_key = conditions.get(cmin)
                cmax_key = conditions.get(cmax)
                if cmin_key is not None:
                    df = df[pd.to_numeric(df[col], errors='coerce') >= cmin_key]
                if cmax_key is not None:
                    df = df[pd.to_numeric(df[col], errors='coerce') <= cmax_key]

        # ROE
        if "ROE" in df.columns and conditions.get("roe_min") is not None:
            df = df[pd.to_numeric(df["ROE"], errors='coerce') >= conditions["roe_min"]]

        # 换手率
        if "换手率" in df.columns and conditions.get("turnover_min") is not None:
            df = df[pd.to_numeric(df["换手率"], errors='coerce') >= conditions["turnover_min"]]

        # 市值（转为亿）
        if "总市值" in df.columns and conditions.get("mcap_min") is not None:
            mcap_min = conditions["mcap_min"] * 1e8
            df = df[pd.to_numeric(df["总市值"], errors='coerce') >= mcap_min]

        # MACD金叉
        if "MACD金叉" in df.columns and conditions.get("ma_cross") in ("golden", "any"):
            cross_col = conditions.get("ma_cross", "")
            if cross_col == "golden":
                df = df[pd.to_numeric(df["MACD金叉"], errors='coerce') > 0]

        # 放量突破
        if "放量突破" in df.columns and conditions.get("vol_break"):
            df = df[pd.to_numeric(df["放量突破"], errors='coerce') > 0]

        # 排序
        sort_by = conditions.get("sort_by", "涨跌幅")
        if sort_by in df.columns:
            ascending = conditions.get("ascending", False)
            df = df.sort_values(sort_by, key=pd.to_numeric,
                                ascending=ascending, na_position='last')

        # 取前N
        top_n = conditions.get("top_n")
        if top_n:
            df = df.head(top_n)

        return df.reset_index(drop=True)

    def scan_strategies(self, df: pd.DataFrame,
                        strategy_results_callback=None) -> pd.DataFrame:
        """
        基于策略扫描结果进一步筛选

        Args:
            df: 股票池 DataFrame（需含'代码'列）
            strategy_results_callback: 回调函数(code, data) -> dict

        Returns:
            含策略命中数的 DataFrame
        """
        if not callable(strategy_results_callback):
            return df

        hits_list = []
        for _, row in df.iterrows():
            code = str(row.get('代码', ''))
            if code:
                try:
                    result = strategy_results_callback(code, None)
                    hit_count = len(result) if isinstance(result, list) else 0
                    hits_list.append(hit_count)
                except Exception:
                    hits_list.append(0)
            else:
                hits_list.append(0)

        df['策略命中'] = hits_list
        return df

    def get_value_picks(self, pe_max: float = 20, pb_max: float = 3,
                         roe_min: float = 15, top_n: int = 30) -> pd.DataFrame:
        """
        经典价值选股:
        PE < pe_max, PB < pb_max, ROE > roe_min
        """
        return self.screen(
            pe_max=pe_max, pb_max=pb_max, roe_min=roe_min,
            sort_by="ROE", top_n=top_n,
        )

    def get_growth_picks(self, pe_max: float = 50,
                          change_min: float = 2,
                          top_n: int = 30) -> pd.DataFrame:
        """
        成长选股:
        PE < 50, 涨幅 > 2%, 按换手率排序
        """
        return self.screen(
            pe_max=pe_max, change_min=change_min,
            sort_by="换手率", top_n=top_n,
        )

    def get_momentum_picks(self, macd_golden: bool = True,
                            vol_break: bool = True,
                            top_n: int = 30) -> pd.DataFrame:
        """
        动量选股:
        MACD金叉 + 放量突破
        """
        df = self.screen(ma_cross="golden" if macd_golden else "any",
                          vol_break=vol_break,
                          sort_by="涨跌幅", top_n=top_n)
        return df

    def format_picks(self, df: pd.DataFrame, cols: list = None,
                      max_cols: int = 6) -> str:
        """格式化选股结果为文本"""
        if df is None or df.empty:
            return "无符合条件的股票"

        if cols is None:
            default = ["代码", "名称", "最新价", "涨跌幅", "PE(TTM)", "ROE"]
            cols = [c for c in default if c in df.columns]

        display_cols = cols[:max_cols]

        lines = [f"选股结果 ({len(df)}只):",
                  "-" * 70]

        for _, row in df.head(30).iterrows():
            parts = []
            for c in display_cols:
                val = row.get(c, '')
                if isinstance(val, float):
                    parts.append(f"{val:.2f}")
                elif isinstance(val, (int, str)):
                    parts.append(str(val))
                else:
                    parts.append('-')
            lines.append("  " + " | ".join(parts))

        if len(df) > 30:
            lines.append(f"  ... 共 {len(df)} 只")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 直接HTTP版选股器（不依赖东财Cookie）
# ═══════════════════════════════════════════════════════════

class SimpleStockScreener:
    """
    简易选股器 — 基于腾讯财经/东财push2
    不需要登录Cookie，基于实时行情数据做简单筛选
    """

    def __init__(self):
        import sys
        sys.path.insert(0, '.')
        import datahub
        self.adapter = datahub

    def screen_by_conditions(self, symbols: list[str],
                              pe_min: float = 0,
                              pe_max: float = 1000,
                              pb_max: float = 100,
                              change_min: float = None,
                              turnover_min: float = None,
                              sort_by: str = "change_pct",
                              top_n: int = 30) -> pd.DataFrame:
        """
        基于实时行情批量筛选

        Args:
            symbols: 股票代码列表
            pe_min/pe_max: PE范围
            pb_max: PB上限
            change_min: 最低涨幅
            turnover_min: 最低换手率
        """
        quotes = self.adapter.quotes(symbols)
        if not quotes:
            return pd.DataFrame()

        rows = []
        for code, q in quotes.items():
            pe = q.get('pe_ttm', 0) or 0
            pb = q.get('pb', 0) or 0
            chg = q.get('change_pct', 0) or 0
            to = q.get('turnover_pct', 0) or 0

            if not (pe_min <= pe <= pe_max):
                continue
            if pb > pb_max and pb_max < 100:
                continue
            if change_min is not None and chg < change_min:
                continue
            if turnover_min is not None and to < turnover_min:
                continue

            rows.append({
                "代码": code,
                "名称": q.get('name', ''),
                "最新价": q.get('price', 0),
                "涨跌幅": chg,
                "PE(TTM)": pe,
                "PB": pb,
                "市值(亿)": q.get('mcap_yi', 0),
                "换手率": to,
            })

        df = pd.DataFrame(rows)
        if not df.empty and sort_by in df.columns:
            df = df.sort_values(sort_by, ascending=False)
        if top_n:
            df = df.head(top_n)

        return df.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("Stock Selection Engine V1.0")
    print("=" * 60)

    # 测试简易选股器
    screener = SimpleStockScreener()

    print("\n📊 测试: PE<30, 涨幅>0, 换手率>0.5%")
    test_symbols = [
        "600519", "000858", "601318", "600036", "000001",
        "002415", "300750", "688017", "300476", "000725",
        "601012", "002594", "300124", "000333", "600900",
    ]

    df = screener.screen_by_conditions(
        test_symbols,
        pe_max=30, change_min=0, turnover_min=0.5,
        top_n=10
    )

    if not df.empty:
        for _, row in df.iterrows():
            print(f"  {row['代码']} {row['名称']:8s} "
                  f"价{row['最新价']:.2f} PE={row['PE(TTM)']:.1f} "
                  f"涨{row['涨跌幅']:.1f}% 换手{row['换手率']:.1f}%")
    else:
        print("  无符合条件的股票")

    print("\n✅ 选股引擎测试完成")
