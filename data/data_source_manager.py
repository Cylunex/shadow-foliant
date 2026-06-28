"""
数据源管理器
实现akshare和tushare的自动切换机制
"""

import os
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
# 2026-06-28 阶段4:K线 8 源链已删,随之去掉其独占的 re/subprocess/json/StringIO 死 import。
try:
    from rate_limiter import throttle as _throttle  # akshare/sina 自限流,防封
except Exception:
    def _throttle(*a, **k):
        return 0.0

# 加载环境变量
load_dotenv()


class DataSourceManager:
    """数据源管理器 - 实现akshare、tushare与a-stock HTTP直连自动切换"""
    
    def __init__(self):
        self.tushare_token = os.getenv('TUSHARE_TOKEN', '')
        self.tushare_available = False
        self.tushare_api = None
        
        # 初始化a-stock HTTP直连适配器（优先数据源，无需依赖安装）
        self.a_stock_available = True
        try:
            from a_stock_data_adapter import adapter
            self.a_stock_adapter = adapter
            print("✅ [a-stock] HTTP直连适配器初始化成功（零第三方数据依赖）")
        except ImportError:
            self.a_stock_adapter = None
            self.a_stock_available = False
            print("ℹ️ [a-stock] HTTP直连适配器未加载，将使用akshare/tushare")
        except Exception as e:
            self.a_stock_adapter = None
            self.a_stock_available = False
            print(f"⚠️ [a-stock] HTTP直连适配器加载失败: {e}")
        
        # 初始化tushare
        if self.tushare_token:
            try:
                import tushare as ts
                ts.set_token(self.tushare_token)
                self.tushare_api = ts.pro_api()
                self.tushare_available = True
                print("✅ Tushare数据源初始化成功")
            except Exception as e:
                print(f"⚠️ Tushare数据源初始化失败: {e}")
                self.tushare_available = False
        else:
            print("ℹ️ 未配置Tushare Token，将仅使用Akshare数据源")
        # ⚠️ adata（二道贩子整合库）已于 2026-06-27 阶段1重构删除：北向走同花顺本地缓存、龙虎榜/资金流走东财直连。

    def _get_proxy_url(self):
        """获取代理URL，优先从环境变量读取，默认使用NAS代理"""
        proxy = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY') or ''
        if not proxy:
            import config as _cfg
            proxy = _cfg.PROXY
        return proxy

    def _clean_proxy_env(self):
        """临时清除代理环境变量（用于国内数据源直连）"""
        self._saved_http_proxy = os.environ.pop('http_proxy', None)
        self._saved_https_proxy = os.environ.pop('https_proxy', None)
        self._saved_HTTP_PROXY = os.environ.pop('HTTP_PROXY', None)
        self._saved_HTTPS_PROXY = os.environ.pop('HTTPS_PROXY', None)

    def _restore_proxy_env(self):
        """恢复代理环境变量"""
        if hasattr(self, '_saved_http_proxy') and self._saved_http_proxy is not None:
            os.environ['http_proxy'] = self._saved_http_proxy
        if hasattr(self, '_saved_https_proxy') and self._saved_https_proxy is not None:
            os.environ['https_proxy'] = self._saved_https_proxy
        if hasattr(self, '_saved_HTTP_PROXY') and self._saved_HTTP_PROXY is not None:
            os.environ['HTTP_PROXY'] = self._saved_HTTP_PROXY
        if hasattr(self, '_saved_HTTPS_PROXY') and self._saved_HTTPS_PROXY is not None:
            os.environ['HTTPS_PROXY'] = self._saved_HTTPS_PROXY

    # ===== K线 8 源降级链已删(2026-06-28 阶段4 收尾)=====================================
    # 旧 get_stock_hist_data(新浪curl→东财curl→akshare→tushare→pywencai→yfinance→Ashare→mootdx)
    # 及其独占助手(_curl_get/_fetch_eastmoney_kline_via_curl/_is_etf_code/_fetch_etf_hist)已删除:
    # K线取数全部收口 datahub.kline(摊平后一层直连原子源 + 三级缓存)。原 2 个非 kline 热路径消费方
    # (market_sentiment_data._calculate_arbr / stock_data._get_chinese_stock_info)已改委托 datahub.kline。
    # ====================================================================================

    def get_stock_basic_info(self, symbol):
        """
        获取股票基本信息（优先akshare，失败时使用tushare）
        
        Args:
            symbol: 股票代码
            
        Returns:
            dict: 股票基本信息
        """
        info = {
            "symbol": symbol,
            "name": "未知",
            "industry": "未知",
            "market": "未知"
        }
        
        # 优先使用yfinance（通过代理，已验证稳定）
        try:
            import yfinance as yf
            proxy_url = self._get_proxy_url()
            
            old_http = os.environ.get('http_proxy')
            old_https = os.environ.get('https_proxy')
            os.environ['http_proxy'] = proxy_url
            os.environ['https_proxy'] = proxy_url
            
            ticker_symbols = [f"{symbol}.SZ", f"{symbol}.SS"]
            for ts in ticker_symbols:
                try:
                    ticker = yf.Ticker(ts)
                    yfinfo = ticker.info or {}
                    if yfinfo.get('longName') or yfinfo.get('shortName'):
                        info['name'] = yfinfo.get('longName') or yfinfo.get('shortName', '未知')
                        info['industry'] = yfinfo.get('industry', '未知')
                        info['market_cap'] = yfinfo.get('marketCap', 0)
                        print(f"[Yfinance] ✅ 通过yfinance获取基本信息")
                        break
                except Exception:
                    continue
            
            if old_http is not None:
                os.environ['http_proxy'] = old_http
            else:
                os.environ.pop('http_proxy', None)
            if old_https is not None:
                os.environ['https_proxy'] = old_https
            else:
                os.environ.pop('https_proxy', None)
                
            if info['name'] != '未知':
                return info
        except Exception as e:
            print(f"[Yfinance] ❌ 获取基本信息失败: {e}")
        
        # yfinance失败，尝试akshare（清理代理后直连）
        try:
            self._clean_proxy_env()
            import akshare as ak
            print(f"[Akshare] 正在获取 {symbol} 的基本信息...")
            
            _throttle('akshare')
            stock_info = ak.stock_individual_info_em(symbol=symbol)
            if stock_info is not None and not stock_info.empty:
                for _, row in stock_info.iterrows():
                    key = row['item']
                    value = row['value']
                    
                    if key == '股票简称':
                        info['name'] = value
                    elif key == '所处行业':
                        info['industry'] = value
                    elif key == '上市时间':
                        info['list_date'] = value
                    elif key == '总市值':
                        info['market_cap'] = value
                    elif key == '流通市值':
                        info['circulating_market_cap'] = value
                
                print(f"[Akshare] ✅ 成功获取基本信息")
                return info
        except Exception as e:
            print(f"[Akshare] ❌ 获取失败: {e}")
        finally:
            self._restore_proxy_env()
        
        # akshare也失败，尝试tushare
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的基本信息（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                df = self.tushare_api.stock_basic(
                    ts_code=ts_code,
                    fields='ts_code,name,area,industry,market,list_date'
                )
                
                if df is not None and not df.empty:
                    info['name'] = df.iloc[0]['name']
                    info['industry'] = df.iloc[0]['industry']
                    info['market'] = df.iloc[0]['market']
                    info['list_date'] = df.iloc[0]['list_date']
                    
                    print(f"[Tushare] ✅ 成功获取基本信息")
                    return info
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        return info
    
    def get_realtime_quotes(self, symbol):
        """
        获取实时行情数据（优先akshare，失败时使用tushare）
        
        Args:
            symbol: 股票代码
            
        Returns:
            dict: 实时行情数据
        """
        quotes = {}
        
        # 优先使用yfinance获取实时行情
        try:
            import yfinance as yf
            proxy_url = self._get_proxy_url()
            old_http = os.environ.get('http_proxy')
            old_https = os.environ.get('https_proxy')
            os.environ['http_proxy'] = proxy_url
            os.environ['https_proxy'] = proxy_url
            
            ticker_symbols = [f"{symbol}.SZ", f"{symbol}.SS"]
            for ts in ticker_symbols:
                try:
                    ticker = yf.Ticker(ts)
                    yfinfo = ticker.info or {}
                    if yfinfo.get('currentPrice') or yfinfo.get('regularMarketPrice'):
                        price = yfinfo.get('currentPrice') or yfinfo.get('regularMarketPrice', 0)
                        quotes = {
                            'symbol': symbol,
                            'name': yfinfo.get('longName') or yfinfo.get('shortName', ''),
                            'price': price,
                            'change_percent': yfinfo.get('regularMarketChangePercent', 0),
                            'change': yfinfo.get('regularMarketChange', 0),
                            'volume': yfinfo.get('volume', 0),
                            'amount': 0,
                            'high': yfinfo.get('regularMarketDayHigh', 0),
                            'low': yfinfo.get('regularMarketDayLow', 0),
                            'open': yfinfo.get('regularMarketOpen', 0),
                            'pre_close': yfinfo.get('regularMarketPreviousClose', 0)
                        }
                        print(f"[Yfinance] ✅ 成功获取实时行情")
                        break
                except Exception:
                    continue
            
            if old_http is not None:
                os.environ['http_proxy'] = old_http
            else:
                os.environ.pop('http_proxy', None)
            if old_https is not None:
                os.environ['https_proxy'] = old_https
            else:
                os.environ.pop('https_proxy', None)
            
            if quotes.get('price'):
                return quotes
        except Exception as e:
            print(f"[Yfinance] ❌ 获取实时行情失败: {e}")
        
        # yfinance失败，尝试akshare（清理代理后直连）
        try:
            self._clean_proxy_env()
            import akshare as ak
            print(f"[Akshare] 正在获取 {symbol} 的实时行情...")
            
            _throttle('akshare')
            df = ak.stock_zh_a_spot_em()
            stock_df = df[df['代码'] == symbol]
            
            if not stock_df.empty:
                row = stock_df.iloc[0]
                quotes = {
                    'symbol': symbol,
                    'name': row['名称'],
                    'price': row['最新价'],
                    'change_percent': row['涨跌幅'],
                    'change': row['涨跌额'],
                    'volume': row['成交量'],
                    'amount': row['成交额'],
                    'high': row['最高'],
                    'low': row['最低'],
                    'open': row['今开'],
                    'pre_close': row['昨收']
                }
                print(f"[Akshare] ✅ 成功获取实时行情")
                return quotes
        except Exception as e:
            print(f"[Akshare] ❌ 获取失败: {e}")
        finally:
            self._restore_proxy_env()
        
        # 所有数据源失败，尝试tushare
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的实时行情（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                df = self.tushare_api.daily(
                    ts_code=ts_code,
                    start_date=datetime.now().strftime('%Y%m%d'),
                    end_date=datetime.now().strftime('%Y%m%d')
                )
                
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    quotes = {
                        'symbol': symbol,
                        'price': row['close'],
                        'change_percent': row['pct_chg'],
                        'volume': row['vol'] * 100,
                        'amount': row['amount'] * 1000,
                        'high': row['high'],
                        'low': row['low'],
                        'open': row['open'],
                        'pre_close': row['pre_close']
                    }
                    print(f"[Tushare] ✅ 成功获取实时行情")
                    return quotes
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        return quotes
    
    def get_financial_data(self, symbol, report_type='income'):
        """
        获取财务数据（优先akshare，失败时使用tushare）
        
        Args:
            symbol: 股票代码
            report_type: 报表类型（'income'利润表, 'balance'资产负债表, 'cashflow'现金流量表）
            
        Returns:
            DataFrame: 财务数据
        """
        # 优先使用akshare（清理代理后调用）
        try:
            self._clean_proxy_env()
            import akshare as ak
            print(f"[Akshare] 正在获取 {symbol} 的财务数据...")
            
            if report_type == 'income':
                _throttle('sina')
                df = ak.stock_financial_report_sina(stock=symbol, symbol="利润表")
            elif report_type == 'balance':
                _throttle('sina')
                df = ak.stock_financial_report_sina(stock=symbol, symbol="资产负债表")
            elif report_type == 'cashflow':
                _throttle('sina')
                df = ak.stock_financial_report_sina(stock=symbol, symbol="现金流量表")
            else:
                df = None
            
            if df is not None and not df.empty:
                print(f"[Akshare] ✅ 成功获取财务数据")
                return df
        except Exception as e:
            print(f"[Akshare] ❌ 获取失败: {e}")
        finally:
            self._restore_proxy_env()
        
        # akshare失败，尝试tushare
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的财务数据（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                
                if report_type == 'income':
                    df = self.tushare_api.income(ts_code=ts_code)
                elif report_type == 'balance':
                    df = self.tushare_api.balancesheet(ts_code=ts_code)
                elif report_type == 'cashflow':
                    df = self.tushare_api.cashflow(ts_code=ts_code)
                else:
                    df = None
                
                if df is not None and not df.empty:
                    print(f"[Tushare] ✅ 成功获取财务数据")
                    return df
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        return None
    
    def _convert_to_ts_code(self, symbol):
        """
        将6位股票代码转换为tushare格式（带市场后缀）
        
        Args:
            symbol: 6位股票代码
            
        Returns:
            str: tushare格式代码（如：000001.SZ）
        """
        if not symbol or len(symbol) != 6:
            return symbol
        
        # 根据代码判断市场
        if symbol.startswith('6'):
            # 上海主板
            return f"{symbol}.SH"
        elif symbol.startswith('0') or symbol.startswith('3'):
            # 深圳主板和创业板
            return f"{symbol}.SZ"
        elif symbol.startswith('8') or symbol.startswith('4'):
            # 北交所
            return f"{symbol}.BJ"
        else:
            # 默认深圳
            return f"{symbol}.SZ"
    
    def _convert_from_ts_code(self, ts_code):
        """
        将tushare格式代码转换为6位代码
        
        Args:
            ts_code: tushare格式代码（如：000001.SZ）
            
        Returns:
            str: 6位股票代码
        """
        if '.' in ts_code:
            return ts_code.split('.')[0]
        return ts_code

    # ============================================================
    # ★ [a-stock] HTTP直连数据源 — 零akshare/tushare依赖版
    # ============================================================

    def get_stock_info_a_stock(self, symbol: str) -> dict:
        """
        [a-stock HTTP直连] 获取个股详细信息（行情+基本面）
        替换: akshare stock_individual_info_em + stock_zh_a_spot_em
        """
        if not self.a_stock_available:
            return {}
        try:
            return self.a_stock_adapter.get_stock_info_detailed(symbol)
        except Exception as e:
            print(f"[a-stock] get_stock_info 失败: {e}")
            return {}

    def get_stock_quote_a_stock(self, symbol: str) -> dict:
        """
        [a-stock HTTP直连] 获取实时行情（含PE/PB/市值/换手率）
        替换: akshare stock_zh_a_spot_em
        """
        if not self.a_stock_available:
            return {}
        try:
            return self.a_stock_adapter.get_quote(symbol)
        except Exception as e:
            print(f"[a-stock] get_quote 失败: {e}")
            return {}

    def get_quotes_a_stock(self, symbols: list[str]) -> dict:
        """
        [a-stock HTTP直连] 批量获取实时行情
        """
        if not self.a_stock_available:
            return {}
        try:
            return self.a_stock_adapter.get_quotes(symbols)
        except Exception as e:
            print(f"[a-stock] get_quotes 失败: {e}")
            return {}

    def get_fund_flow_a_stock(self, symbol: str, days: int = 120) -> list[dict]:
        """
        [a-stock HTTP直连] 获取历史资金流向（日级）
        替换: akshare stock_individual_fund_flow
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_fund_flow_history(symbol, days)
        except Exception as e:
            print(f"[a-stock] get_fund_flow 失败: {e}")
            return []

    def get_hot_stocks_a_stock(self, date: str = None) -> pd.DataFrame:
        """
        [a-stock HTTP直连] 获取当日强势股 + 题材归因
        替换: akshare 的各种概念/热点查询
        """
        if not self.a_stock_available:
            return pd.DataFrame()
        try:
            return self.a_stock_adapter.get_hot_stocks(date)
        except Exception as e:
            print(f"[a-stock] get_hot_stocks 失败: {e}")
            return pd.DataFrame()

    def get_industry_ranking_a_stock(self, top_n: int = 20) -> dict:
        """
        [a-stock HTTP直连] 获取行业板块排名
        替换: akshare stock_board_industry_name_em
        """
        if not self.a_stock_available:
            return {"top": [], "bottom": [], "total": 0}
        try:
            return self.a_stock_adapter.get_industry_ranking(top_n)
        except Exception as e:
            print(f"[a-stock] get_industry_ranking 失败: {e}")
            return {"top": [], "bottom": [], "total": 0}

    def get_concept_ranking_a_stock(self, top_n: int = 50) -> dict:
        """
        [a-stock HTTP直连] 获取概念板块排名（东财 push2 m:90+t:3）
        替换: akshare stock_board_concept_name_em
        """
        if not self.a_stock_available:
            return {"top": [], "bottom": [], "total": 0}
        try:
            return self.a_stock_adapter.get_concept_ranking(top_n)
        except Exception as e:
            print(f"[a-stock] get_concept_ranking 失败: {e}")
            return {"top": [], "bottom": [], "total": 0}

    def get_sector_fund_flow_a_stock(self, sector_type: str = "industry", top_n: int = 50) -> list[dict]:
        """
        [a-stock HTTP直连] 板块资金流（行业或概念，东财 push2，零鉴权）
        替换: akshare stock_sector_fund_flow_rank
        sector_type: 'industry' / 'concept'
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_sector_fund_flow(sector_type, top_n)
        except Exception as e:
            print(f"[a-stock] get_sector_fund_flow({sector_type}) 失败: {e}")
            return []

    def get_concept_blocks_a_stock(self, symbol: str) -> dict:
        """
        [a-stock HTTP直连] 获取概念板块归属
        替换: akshare stock_board_concept_name_em
        """
        if not self.a_stock_available:
            return {"industry": [], "concept": [], "region": [], "concept_tags": []}
        try:
            return self.a_stock_adapter.get_concept_blocks(symbol)
        except Exception as e:
            print(f"[a-stock] get_concept_blocks 失败: {e}")
            return {"industry": [], "concept": [], "region": [], "concept_tags": []}

    # get_financial_reports_a_stock 已删(2026-06-28 阶段4):datahub.financials 走
    # sources/sina.financials,本 orphan 包装(→ adapter._sina_financial_report 读错层级)无调用方。

    def get_stock_news_a_stock(self, symbol: str, page_size: int = 20) -> list[dict]:
        """
        [a-stock HTTP直连] 获取个股新闻
        替换: akshare stock_news_em
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_stock_news(symbol, page_size)
        except Exception as e:
            print(f"[a-stock] get_stock_news 失败: {e}")
            return []

    def get_market_news_a_stock(self, page_size: int = 50) -> list[dict]:
        """
        [a-stock HTTP直连] 获取财联社全市场快讯
        替换: akshare stock_info_global_cls
        兜底: CLS 失效时降级到 akshare 东财全球快讯
        """
        if self.a_stock_available:
            try:
                news = self.a_stock_adapter.get_market_news(page_size)
                if news:
                    return news
            except Exception as e:
                print(f"[a-stock] get_market_news 失败: {e}")
        # CLS 失效 → 降级到 akshare 东财全球快讯
        try:
            import akshare as ak
            df = ak.stock_info_global_em()
            if df is not None and not df.empty:
                rows = []
                for _, r in df.head(page_size).iterrows():
                    rows.append({
                        "title": str(r.get("标题", "")),
                        "content": str(r.get("摘要", "")),
                        "time": str(r.get("发布时间", "")),
                    })
                return rows
        except Exception as e:
            print(f"[a-stock] akshare 快讯降级也失败: {e}")
        return []

    def get_margin_trading_a_stock(self, symbol: str, page_size: int = 30) -> list[dict]:
        """
        [a-stock HTTP直连] 获取融资融券明细
        替换: akshare stock_margin_detail_sse
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_margin_trading(symbol, page_size)
        except Exception as e:
            print(f"[a-stock] get_margin_trading 失败: {e}")
            return []

    def get_dragon_tiger_a_stock(self, symbol: str, trade_date: str = None, look_back: int = 30) -> dict:
        """
        [a-stock HTTP直连] 龙虎榜数据
        替换: akshare stock_lh_em
        """
        if not self.a_stock_available:
            return {"records": [], "seats": {"buy": [], "sell": []}, "institution": {}}
        try:
            return self.a_stock_adapter.get_dragon_tiger(symbol, trade_date, look_back)
        except Exception as e:
            print(f"[a-stock] get_dragon_tiger 失败: {e}")
            return {"records": [], "seats": {"buy": [], "sell": []}, "institution": {}}

    def get_valuation_a_stock(self, symbol: str) -> dict:
        """
        [a-stock HTTP直连] 单票完整估值分析
        替换: akshare 估值相关全套查询
        """
        if not self.a_stock_available:
            return {}
        try:
            return self.a_stock_adapter.full_valuation(symbol)
        except Exception as e:
            print(f"[a-stock] get_valuation 失败: {e}")
            return {}

    def get_full_valuation_a_stock(self, symbol: str) -> dict:
        """
        [a-stock HTTP直连] 完整估值 — 前向PE / PEG / PE消化年数（30x 锚点）
        基于腾讯实时行情 + 同花顺一致预期EPS
        返回字段：price, pe_ttm, pb, eps_cur, eps_next, pe_fwd, cagr_pct, peg, digest_years, analyst_count
        """
        if not self.a_stock_available:
            return {}
        try:
            return self.a_stock_adapter.get_full_valuation(symbol)
        except Exception as e:
            print(f"[a-stock] get_full_valuation 失败: {e}")
            return {}

    def get_eps_forecast_a_stock(self, symbol: str) -> pd.DataFrame:
        """
        [a-stock HTTP直连] 同花顺一致预期EPS
        替换: akshare stock_profit_forecast
        """
        if not self.a_stock_available:
            return pd.DataFrame()
        try:
            return self.a_stock_adapter.get_eps_forecast(symbol)
        except Exception as e:
            print(f"[a-stock] get_eps_forecast 失败: {e}")
            return pd.DataFrame()

    def get_holder_num_change_a_stock(self, symbol: str) -> list[dict]:
        """
        [a-stock HTTP直连] 股东户数变化
        替换: akshare stock_holder_number
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_holder_num_change(symbol)
        except Exception as e:
            print(f"[a-stock] get_holder_num_change 失败: {e}")
            return []

    def get_block_trade_a_stock(self, symbol: str) -> list[dict]:
        """
        [a-stock HTTP直连] 大宗交易
        替换: akshare stock_block_trade_em
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_block_trade(symbol)
        except Exception as e:
            print(f"[a-stock] get_block_trade 失败: {e}")
            return []

    def get_lockup_expiry_a_stock(self, symbol: str, trade_date: str = None) -> dict:
        """
        [a-stock HTTP直连] 限售解禁日历
        替换: akshare stock_lockup
        """
        if not self.a_stock_available:
            return {"history": [], "upcoming": []}
        try:
            return self.a_stock_adapter.get_lockup_expiry(symbol, trade_date)
        except Exception as e:
            print(f"[a-stock] get_lockup_expiry 失败: {e}")
            return {"history": [], "upcoming": []}

    def get_dividend_history_a_stock(self, symbol: str) -> list[dict]:
        """
        [a-stock HTTP直连] 分红送转历史
        替换: akshare stock_dividents
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_dividend_history(symbol)
        except Exception as e:
            print(f"[a-stock] get_dividend_history 失败: {e}")
            return []

    def get_announcements_a_stock(self, symbol: str) -> list[dict]:
        """
        [a-stock HTTP直连] 巨潮公告检索
        替换: akshare stock_notice
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_announcements(symbol)
        except Exception as e:
            print(f"[a-stock] get_announcements 失败: {e}")
            return []

    # ============================================================
    # 北向资金（同花顺本地缓存主源；2026-06-27 阶段1:adata 兜底已删）
    # 龙虎榜详情/个股资金流/融资融券的 adata 方法已删除：
    #   · dragon_tiger → datahub.dragon_tiger（东财数据中心直连）
    #   · capital_flow → datahub.capital_flow（东财 push2his 直连）
    # ============================================================

    def get_north_flow_a_data(self, days: int = 30):
        """北向资金日度数据 — 沪股通/深股通 净流入累计

        数据源优先级：
          1. northbound_cache 本地缓存（同花顺 hsgtApi 真实数据，由 jobs_hub 每日 15:40 追加）
          2. 同花顺实时拉取一次并入库（缓存为空时的 fallback）

        返回: list[dict] 含 trade_date, hgt_yi, sgt_yi, net_total（亿元）+
              兼容字段 net_hgt, net_sgt（元）, net_tgt（恒 0）；无缓存且实时拉取失败 → []。
        """
        try:
            from northbound_cache import get_recent, refresh_today
            rows = get_recent(days)
            if rows:
                return rows
            if refresh_today():
                return get_recent(days)
        except Exception as e:
            print(f"[northbound_cache] 读取失败: {e}")
        return []


# 全局数据源管理器实例
data_source_manager = DataSourceManager()

