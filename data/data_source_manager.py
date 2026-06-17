"""
数据源管理器
实现akshare和tushare的自动切换机制
"""

import os
import re
import subprocess
import json
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from io import StringIO
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

        # 初始化 adata（北向资金、龙虎榜详情、概念资金流等 akshare 缺失/不稳定的数据源）
        self.adata_available = False
        try:
            import adata  # noqa: F401
            self.adata_available = True
            print("✅ [adata] 数据源初始化成功（北向资金/龙虎榜/概念资金流）")
        except ImportError:
            print("ℹ️ [adata] 未安装，相关接口将不可用（pip install adata 可启用）")
        except Exception as e:
            print(f"⚠️ [adata] 初始化失败: {e}")

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

    def _curl_get(self, url, proxy=None):
        """
        使用curl命令获取数据（绕过Python requests库的代理兼容性问题）
        
        Args:
            url: 请求URL
            proxy: 代理地址，None表示不使用代理
            
        Returns:
            str: 响应内容，失败返回None
        """
        try:
            _throttle('eastmoney')  # curl 直连东财自限流,防封
            cmd = ['curl', '-s', '--connect-timeout', '10', '--max-time', '20']
            cmd += ['-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36']
            cmd += ['-H', 'Referer: https://quote.eastmoney.com/']
            if proxy:
                cmd += ['--proxy', proxy]
            else:
                cmd += ['--noproxy', '*']
            cmd.append(url)
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            return None
        except Exception as e:
            print(f"[Curl] ❌ 请求失败: {e}")
            return None

    def _fetch_eastmoney_kline_via_curl(self, symbol, start_date, end_date, adjust='qfq', proxy=None):
        """
        通过curl命令从东方财富获取K线数据
        """
        # 判断市场
        secid_prefix = '0' if symbol.startswith('0') or symbol.startswith('3') else '1'
        secid_prefix = '1' if symbol.startswith('6') else secid_prefix
        secid_prefix = '0' if symbol.startswith('159') else secid_prefix  # ETF基金
        
        # 标准化日期
        start = start_date.replace('-', '') if start_date else '20250101'
        end = end_date.replace('-', '') if end_date else datetime.now().strftime('%Y%m%d')
        
        url = (
            f'https://push2his.eastmoney.com/api/qt/stock/kline/get'
            f'?fields1=f1,f2,f3,f4,f5,f6'
            f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116'
            f'&ut=7eea3edcaed734bea9cbfc24409ed989'
            f'&klt=101&fqt=1'
            f'&secid={secid_prefix}.{symbol}'
            f'&beg={start}&end={end}'
        )
        
        retries = 3
        for attempt in range(retries):
            response = self._curl_get(url, proxy=proxy)
            if response:
                try:
                    data = json.loads(response)
                    if data and data.get('data') and data['data'].get('klines'):
                        klines = data['data']['klines']
                        records = []
                        for kline in klines:
                            parts = kline.split(',')
                            if len(parts) >= 11:
                                records.append({
                                    'date': parts[0],
                                    'open': float(parts[1]),
                                    'close': float(parts[2]),
                                    'high': float(parts[3]),
                                    'low': float(parts[4]),
                                    'volume': float(parts[5]),
                                    'amount': float(parts[6]),
                                    'amplitude': float(parts[7]) if parts[7] else 0,
                                    'pct_change': float(parts[8]) if parts[8] else 0,
                                    'change': float(parts[9]) if parts[9] else 0,
                                    'turnover': float(parts[10]) if parts[10] else 0
                                })
                        if records:
                            df = pd.DataFrame(records)
                            df['date'] = pd.to_datetime(df['date'])
                            return df
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    print(f"[Curl-东方财富] ⚠️ 数据解析失败(尝试{attempt+1}/{retries}): {e}")
                    continue
            
        return None

    def get_stock_hist_data(self, symbol, start_date=None, end_date=None, adjust='qfq'):
        """
        获取股票历史数据（A股优先腾讯/EastMoney HTTP → akshare/tushare → pywencai → yahoo）
        
        Args:
            symbol: 股票代码（6位数字）
            start_date: 开始日期（格式：'20240101'或'2024-01-01'）
            end_date: 结束日期
            adjust: 复权类型（'qfq'前复权, 'hfq'后复权, ''不复权）
        """
        if start_date:
            start_date = start_date.replace('-', '')
        if end_date:
            end_date = end_date.replace('-', '')
        else:
            end_date = datetime.now().strftime('%Y%m%d')
        proxy_url = self._get_proxy_url()
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')
        
        # ⭐ A股判别：6位数字=沪深京，Yahoo对A股没有数据直接跳过
        is_a_stock = bool(re.match(r'^[0-9]{6}$', str(symbol)))
        
        # ========== 策略1: 新浪日K（curl无代理，已验证可用）==========
        # ETF 代码(51xxxx/15xxxx)和股票一样走这条; 6 位数字 prefix 规则 sh/sz 也覆盖
        # 沪市 ETF (51xxxx 走 sh) 和 深市 ETF (15xxxx 走 sz)。
        if is_a_stock:
            sina_fail_reason = None
            try:
                import subprocess
                prefix = 'sh' if symbol.startswith('6') else 'sz'
                datalen = 365
                url = f'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{symbol}=/CN_MarketData.getKLineData?symbol={prefix}{symbol}&scale=240&ma=no&datalen={datalen}'
                r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15',
                                    '--noproxy', '*',
                                    '-H', 'User-Agent: Mozilla/5.0', url],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    sina_fail_reason = f'curl rc={r.returncode}'
                elif not r.stdout:
                    sina_fail_reason = 'curl 响应空'
                else:
                    m = re.search(r'\[.*\]', r.stdout)
                    if not m:
                        sina_fail_reason = '响应无 JSON 数组(可能反爬/非交易代码)'
                    else:
                        raw = json.loads(m.group())
                        rows = []
                        for item in raw:
                            try:
                                rows.append({'date': item['day'], 'open': float(item['open']),
                                             'high': float(item['high']), 'low': float(item['low']),
                                             'close': float(item['close']), 'volume': float(item['volume'])})
                            except (ValueError, KeyError):
                                continue
                        if rows:
                            df = pd.DataFrame(rows)
                            df['date'] = pd.to_datetime(df['date'])
                            df = df.sort_values('date')
                            # 成功不打日志(常态), 失败才打 ⚠️ — 否则 kline_prefetch
                            # 焐 354 只股票 = 几百行 ✅ 刷屏(2026-06-17 调整)
                            return df
                        sina_fail_reason = '解析后无有效行'
            except Exception as e:
                sina_fail_reason = f'{type(e).__name__}: {str(e)[:80]}'
            if sina_fail_reason:
                # 静默 fall through 加 ⚠️ 一行(成功路径不打),方便后续诊断为什么走到 Akshare
                print(f'[新浪日K] ⚠️ {symbol} 失败 ({sina_fail_reason}), 转 curl-东财')

            # 策略1b: curl东财日K（无代理，作为新浪的备选）
            try:
                df = self._fetch_eastmoney_kline_via_curl(symbol, start_date, end_date, adjust, proxy=None)
                if df is not None and not df.empty:
                    # 成功不打日志(走到这条说明新浪挂了,东财兜底成功是常态)
                    return df
                print(f'[curl-东财] ⚠️ {symbol} 返回空, 转 Akshare')
            except Exception as e:
                print(f"[curl-东财] ⚠️ {symbol} 失败 ({type(e).__name__}: {str(e)[:80]}), 转 Akshare")

        # ========== 策略2: akshare（限流后直连）==========
        try:
            self._clean_proxy_env()
            import akshare as ak
            print(f"[Akshare] 正在获取 {symbol} 的历史数据...")
            _throttle('akshare')
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )
            if df is not None and not df.empty:
                df = df.rename(columns={
                    '日期': 'date', '开盘': 'open', '收盘': 'close',
                    '最高': 'high', '最低': 'low', '成交量': 'volume',
                    '成交额': 'amount', '振幅': 'amplitude',
                    '涨跌幅': 'pct_change', '涨跌额': 'change', '换手率': 'turnover'
                })
                df['date'] = pd.to_datetime(df['date'])
                return df
        except Exception as e:
            print(f"[Akshare] ❌ 获取失败: {e}")
        finally:
            self._restore_proxy_env()
        
        # ========== 策略3: tushare ==========
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的历史数据...")
                ts_code = self._convert_to_ts_code(symbol)
                adj_dict = {'qfq': 'qfq', 'hfq': 'hfq', '': None}
                adj = adj_dict.get(adjust, 'qfq')
                df = self.tushare_api.daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date, adj=adj
                )
                if df is not None and not df.empty:
                    df = df.rename(columns={
                        'trade_date': 'date', 'vol': 'volume', 'amount': 'amount'
                    })
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date')
                    df['volume'] = df['volume'] * 100
                    df['amount'] = df['amount'] * 1000
                    return df
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        # ========== 策略4: pywencai（同花顺，限流较严，偶尔报错）==========
        try:
            from data.pywencai_safe import pywencai_get
            print(f"[Pywencai] 正在获取 {symbol} 的历史数据...")
            query = f"股票代码{symbol}的日线行情"
            result = pywencai_get(query, timeout=60)
            if result is not None and isinstance(result, dict):
                for key in result:
                    df = result[key]
                    if isinstance(df, pd.DataFrame) and len(df) > 0 and '收盘' in [str(c) for c in df.columns]:
                        return df
            print(f"[Pywencai] ❌ 返回数据格式异常")
        except ImportError:
            print(f"[Pywencai] ❌ pywencai 未安装")
        except Exception as e:
            print(f"[Pywencai] ❌ 获取失败: {e}")
        
        # ========== 策略5: yfinance（仅非A股，A股永远没有数据跳过）==========
        if not is_a_stock:
            try:
                import yfinance as yf
                old_http = os.environ.get('http_proxy')
                old_https = os.environ.get('https_proxy')
                os.environ['http_proxy'] = proxy_url
                os.environ['https_proxy'] = proxy_url
                print(f"[Yfinance] 正在获取 {symbol} 的历史数据...")
                ticker_symbols = [f"{symbol}.SZ", f"{symbol}.SS", f"{symbol}"]
                df = None
                for ts in ticker_symbols:
                    try:
                        ticker = yf.Ticker(ts)
                        df = ticker.history(period='1y')
                        if df is not None and not df.empty:
                            break
                        else:
                            df = None
                    except Exception:
                        continue
                if old_http: os.environ['http_proxy'] = old_http
                else: os.environ.pop('http_proxy', None)
                if old_https: os.environ['https_proxy'] = old_https
                else: os.environ.pop('https_proxy', None)
                if df is not None and not df.empty:
                    df = df.reset_index()
                    df = df.rename(columns={
                        'Date': 'date', 'Open': 'open', 'Close': 'close',
                        'High': 'high', 'Low': 'low', 'Volume': 'volume'
                    })
                    df['date'] = pd.to_datetime(df['date'])
                    if start_date:
                        s = pd.to_datetime(start_date, utc=True)
                        if df['date'].dt.tz is None:
                            df = df[df['date'] >= pd.to_datetime(start_date)]
                        else:
                            df['date_utc'] = df['date'].dt.tz_convert('UTC')
                            df = df[df['date_utc'] >= s].drop(columns=['date_utc'])
                    return df
            except ImportError:
                pass
            except Exception as e:
                print(f"[Yfinance] ❌ 获取失败: {e}")
        else:
            print(f"[Yfinance] ⏭️ 跳过(Yahoo无A股数据)")
        
        # ========== 策略6: Ashare 零依赖兜底（腾讯/新浪,qfq 日线;可移植）==========
        if is_a_stock:
            try:
                from ashare_fallback import get_price as _ashare_get
                n = 250
                if start_date:
                    try:
                        n = max(60, (datetime.now() - datetime.strptime(start_date, '%Y%m%d')).days)
                    except Exception:
                        pass
                df = _ashare_get(symbol, count=min(n, 1500), frequency='1d')
                if df is not None and len(df) > 0:
                    if start_date:
                        df = df[df['date'] >= pd.to_datetime(start_date)]
                    return df
            except Exception as e:
                print(f"[Ashare兜底] ❌ 获取失败: {e}")

        # ========== 策略7: mootdx 通达信公网（raw 日线;可移植,无需内网服务）==========
        if is_a_stock:
            try:
                from tdx_mootdx import get_kline as _tdx_k
                df = _tdx_k(symbol, frequency='day', count=800, adjust=adjust)
                if df is not None and len(df) > 0:
                    if start_date:
                        df = df[df['date'] >= pd.to_datetime(start_date)]
                    return df
            except Exception as e:
                print(f"[mootdx-TDX] ❌ 获取失败: {e}")

        print("❌ 所有数据源均获取失败")
        return None

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

    def get_financial_reports_a_stock(self, symbol: str, report_type: str = "lrb") -> list[dict]:
        """
        [a-stock HTTP直连] 获取新浪财报三表
        替换: akshare stock_financial_report_sina
        report_type: fzb=资产负债表 lrb=利润表 llb=现金流量表
        """
        if not self.a_stock_available:
            return []
        try:
            return self.a_stock_adapter.get_financial_reports(symbol, report_type)
        except Exception as e:
            print(f"[a-stock] get_financial_reports 失败: {e}")
            return []

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
    # adata 数据源 — 填补 akshare 缺失的数据（北向/龙虎榜详情/概念资金流）
    # ============================================================

    def get_north_flow_a_data(self, days: int = 30):
        """北向资金日度数据 — 沪股通/深股通 净流入累计

        数据源优先级：
          1. northbound_cache 本地缓存（同花顺 hsgtApi 真实数据，由 jobs_hub 每日 15:40 追加）
          2. 同花顺实时拉取一次并入库（缓存为空时的 fallback）
          3. adata（断供期间返回 0，仅占位）

        返回: list[dict] 含 trade_date, hgt_yi, sgt_yi, net_total（亿元）+
              兼容字段 net_hgt, net_sgt（元）, net_tgt（恒 0）
        """
        try:
            from northbound_cache import get_recent, refresh_today
            rows = get_recent(days)
            if rows:
                return rows
            if refresh_today():
                return get_recent(days)
        except Exception as e:
            print(f"[northbound_cache] 读取失败，回退 adata: {e}")

        if not self.adata_available:
            return []
        try:
            import adata
            df = adata.sentiment.north.north_flow()
            if df is None or df.empty:
                return []
            df = df.head(days)
            return df.to_dict(orient='records')
        except Exception as e:
            print(f"[adata] get_north_flow 失败: {e}")
            return []

    def get_dragon_tiger_detail_a_data(self, trade_date: str = None):
        """龙虎榜每日详情（adata 比 akshare 更稳定）

        参数: trade_date 'YYYY-MM-DD'，None 则取最近交易日
        返回: list[dict] 每条含股票代码、名称、上榜原因、买卖席位汇总
        """
        if not self.adata_available:
            return []
        try:
            import adata
            df = adata.sentiment.hot.list_a_list_daily(trade_date=trade_date) if trade_date \
                else adata.sentiment.hot.list_a_list_daily()
            if df is None or df.empty:
                return []
            return df.to_dict(orient='records')
        except Exception as e:
            print(f"[adata] get_dragon_tiger_detail 失败: {e}")
            return []

    def get_capital_flow_a_data(self, symbol: str):
        """个股历史日度资金流（adata 备用，避免 akshare 限频）

        参数: symbol 6 位股票代码（不带前缀）
        返回: list[dict] 主力/超大/大/中/小 各档净流入
        """
        if not self.adata_available:
            return []
        try:
            import adata
            df = adata.stock.market.get_capital_flow(stock_code=symbol)
            if df is None or df.empty:
                return []
            return df.to_dict(orient='records')
        except Exception as e:
            print(f"[adata] get_capital_flow 失败: {e}")
            return []

    def get_securities_margin_a_data(self, symbol: str = None):
        """融资融券数据（akshare 不提供个股粒度，adata 可以）

        参数: symbol 6 位股票代码，None 取大盘数据
        返回: list[dict]
        """
        if not self.adata_available:
            return []
        try:
            import adata
            df = adata.sentiment.securities_margin(stock_code=symbol) if symbol \
                else adata.sentiment.securities_margin()
            if df is None or df.empty:
                return []
            return df.to_dict(orient='records')
        except Exception as e:
            print(f"[adata] get_securities_margin 失败: {e}")
            return []


# 全局数据源管理器实例
data_source_manager = DataSourceManager()

