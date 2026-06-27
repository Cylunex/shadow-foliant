import time
import threading
import schedule
from datetime import datetime, timedelta
from typing import Dict, List
import os
import logging

from monitor_db import monitor_db
from stock_data import StockDataFetcher
from notification_service import notification_service
# 量化实盘下单(miniqmt/xtquant)已下线(2026-06-13):监控/告警保留,下单待重接时再写。

# 导入TDX数据源（如果可用）
try:
    from smart_monitor_tdx_data import SmartMonitorTDXDataFetcher
    TDX_AVAILABLE = True
except ImportError:
    TDX_AVAILABLE = False
    logging.warning("TDX数据源模块未找到，将使用默认数据源")

def _coerce_naive_dt(value):
    """把 last_checked 统一成 naive datetime。
    兼容:PG TIMESTAMPTZ 返回的 datetime(可能带 tz)、SQLite 的 ISO 字符串。
    无法解析则返回 None(当作从未检查 → 需要更新)。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    # 去掉时区,与 datetime.now()(naive)可比较
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# 各监测股上一轮价格(进程内),供告警"穿越"判断降噪。key=stock id
_PREV_PRICES: dict = {}


class StockMonitorService:
    """股票监测服务"""
    
    def __init__(self):
        self.fetcher = StockDataFetcher()
        
        # 初始化TDX数据源（如果启用）
        self.tdx_fetcher = None
        self.use_tdx = False
        
        # 从环境变量获取TDX配置
        tdx_enabled = os.getenv('TDX_ENABLED', 'false').lower() == 'true'
        tdx_base_url = os.getenv('TDX_BASE_URL', 'http://127.0.0.1:8181')
        
        if tdx_enabled and TDX_AVAILABLE:
            try:
                self.tdx_fetcher = SmartMonitorTDXDataFetcher(base_url=tdx_base_url)
                self.use_tdx = True
                logging.info(f"✅ TDX数据源已启用: {tdx_base_url}")
            except Exception as e:
                logging.warning(f"TDX数据源初始化失败，将使用默认数据源: {e}")
        
        self.running = False
        self.thread = None
    
    def get_detailed_status(self) -> dict:
        """获取详细状态 — 区分多种"停止"原因，UI 显示更清晰

        Returns: {
            'state': 'running' / 'no_stocks' / 'off_hours' / 'stopped',
            'label': UI 显示文本,
            'color': 'green' / 'gray' / 'orange' / 'red',
            'detail': 详细说明,
        }
        """
        try:
            from monitor_db import monitor_db
            stocks = monitor_db.get_monitored_stocks() or []
            stock_count = len(stocks)
        except Exception:
            stock_count = 0

        # 1. 服务在跑
        if self.running:
            if stock_count == 0:
                return {
                    'state': 'running_empty',
                    'label': '🟡 运行中（无股票）',
                    'color': 'orange',
                    'detail': f'服务线程在轮询，但监测列表为空（{stock_count} 只）',
                }
            return {
                'state': 'running',
                'label': '🟢 运行中',
                'color': 'green',
                'detail': f'监测 {stock_count} 只股票，每 5 分钟检查一次',
            }

        # 2. 服务停了 — 判断原因
        if stock_count == 0:
            return {
                'state': 'no_stocks',
                'label': '⚪ 无监测股票',
                'color': 'gray',
                'detail': '监测列表为空，无需启动服务',
            }

        # 3. 列表有股票但停了 — 看是不是非交易时段
        try:
            from monitor_scheduler import get_scheduler
            sched = get_scheduler()
            if sched and sched.running:
                # 调度器在跑 — 说明是它停的（交易时段自动启停）
                from datetime import datetime
                now = datetime.now()
                weekday = now.weekday()  # 0=Mon, 6=Sun
                if weekday >= 5:
                    next_msg = '下个交易日 09:30 自动启动'
                else:
                    h, m = now.hour, now.minute
                    if h < 9 or (h == 9 and m < 30):
                        next_msg = '今日 09:30 自动启动'
                    elif (h == 11 and m >= 30) or h == 12:
                        next_msg = '今日 13:00 自动启动'
                    elif h >= 15:
                        next_msg = '下个交易日 09:30 自动启动'
                    else:
                        next_msg = '应在交易时段，scheduler 配置可能异常'
                return {
                    'state': 'off_hours',
                    'label': '🟡 非交易时段',
                    'color': 'orange',
                    'detail': next_msg,
                }
        except Exception:
            pass

        # 4. 用户手动停的（或其他未知）
        return {
            'state': 'stopped',
            'label': '🔴 已停止',
            'color': 'red',
            'detail': f'监测列表有 {stock_count} 只股票，但服务未运行',
        }

    def _ui_message(self, level: str, msg: str):
        """输出消息 —— 原走 Streamlit st.*,2026-06 迁 FastAPI 后 Streamlit UI 已废,统一 print。"""
        print(f'[monitor_service] {msg}')

    def start_monitoring(self, force: bool = False):
        """启动监测服务

        Args:
            force: 强制启动（即使列表为空）。默认 False — 列表空时跳过启动省 CPU
        """
        if self.running:
            return

        # 列表为空时默认不起线程（省 CPU；后续添加股票时会再判断）
        if not force:
            try:
                from monitor_db import monitor_db
                if not (monitor_db.get_monitored_stocks() or []):
                    self._ui_message('info', 'ℹ️ 监测列表为空，跳过启动监测线程')
                    return
            except Exception:
                pass

        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        self._ui_message('success', '✅ 监测服务已启动')

    def stop_monitoring(self):
        """停止监测服务"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self._ui_message('info', '⏹️ 监测服务已停止')
    
    def _monitor_loop(self):
        """监测循环"""
        print("监测服务已启动")
        while self.running:
            try:
                self._check_all_stocks()
                # 根据最小监测间隔决定循环间隔，最少5分钟检查一次
                time.sleep(300)  # 每5分钟检查一次
            except Exception as e:
                print(f"监测服务错误: {e}")
                time.sleep(60)  # 错误后等待1分钟再重试
    
    def _check_all_stocks(self):
        """检查所有监测股票"""
        stocks = monitor_db.get_monitored_stocks()
        current_time = datetime.now()
        
        updated_count = 0
        for stock in stocks:
            # 检查是否需要更新价格
            last_checked = stock.get('last_checked')
            check_interval = stock.get('check_interval', 30)
            
            last_checked_dt = _coerce_naive_dt(last_checked)
            if last_checked_dt:
                next_check = last_checked_dt + timedelta(minutes=check_interval)
                if current_time < next_check:
                    # 显示距离下次检查的时间
                    time_left = (next_check - current_time).total_seconds() / 60
                    print(f"股票 {stock['symbol']} 距离下次检查还有 {time_left:.1f} 分钟")
                    continue
            
            try:
                print(f"正在更新股票 {stock['symbol']} 的价格...")
                self._update_stock_price(stock)
                updated_count += 1
                
                # 在每个股票请求之间增加延迟，避免API限流
                if updated_count < len(stocks):
                    time.sleep(3)  # 每个股票之间等待3秒
            except Exception as e:
                print(f"❌ 更新股票 {stock['symbol']} 价格失败: {e}")
                time.sleep(3)  # 失败后也等待3秒再继续
        
        if updated_count > 0:
            print(f"✅ 本轮共更新了 {updated_count} 只股票")
    
    def _update_stock_price(self, stock: Dict):
        """更新股票价格并检查条件"""
        symbol = stock['symbol']
        current_price = None
        
        # 获取最新价格
        try:
            # 优先使用TDX数据源（如果已启用且为A股）
            if self.use_tdx and self._is_a_stock(symbol):
                print(f"🔄 使用TDX数据源获取 {symbol} 行情...")
                quote = self.tdx_fetcher.get_realtime_quote(symbol)
                
                if quote and quote.get('current_price'):
                    current_price = float(quote['current_price'])
                    print(f"✅ TDX获取成功: {symbol} 当前价格: ¥{current_price}")
                else:
                    # TDX失败，降级到默认数据源
                    print(f"⚠️ TDX获取失败，降级到默认数据源: {symbol}")
                    current_price = self._get_price_from_default_source(symbol)
            else:
                # 使用默认数据源（AKShare/yfinance）
                current_price = self._get_price_from_default_source(symbol)
            
            # 处理获取到的价格
            if current_price and current_price > 0:
                try:
                    current_price = float(current_price)
                    # 更新数据库（包括更新last_checked时间）
                    monitor_db.update_stock_price(stock['id'], current_price)
                    print(f"✅ {symbol} 当前价格: ¥{current_price}")
                    
                    # 检查触发条件
                    self._check_trigger_conditions(stock, current_price)
                except (ValueError, TypeError) as e:
                    print(f"❌ 股票 {symbol} 价格格式错误: {current_price}")
                    # 即使失败也更新last_checked，避免持续重试
                    monitor_db.update_last_checked(stock['id'])
            else:
                print(f"⚠️ 无法获取股票 {symbol} 的当前价格")
                # 更新last_checked，避免持续重试
                monitor_db.update_last_checked(stock['id'])
                
        except Exception as e:
            print(f"❌ 获取股票 {symbol} 数据失败: {e}")
            # 即使失败也更新last_checked，避免持续重试
            try:
                monitor_db.update_last_checked(stock['id'])
            except:
                pass
    
    def _is_a_stock(self, symbol: str) -> bool:
        """判断是否为A股（6位数字）"""
        return symbol.isdigit() and len(symbol) == 6
    
    def _get_price_from_default_source(self, symbol: str) -> float:
        """从默认数据源获取价格"""
        try:
            stock_info = self.fetcher.get_stock_info(symbol)
            current_price = stock_info.get('current_price')
            
            if current_price and current_price != 'N/A':
                return float(current_price)
            return None
        except Exception as e:
            print(f"默认数据源获取失败: {e}")
            return None
    
    def _check_trigger_conditions(self, stock: Dict, current_price: float):
        """检查触发条件"""
        if not stock.get('notification_enabled', True):
            return

        entry_range = stock.get('entry_range', {})
        take_profit = stock.get('take_profit')
        stop_loss = stock.get('stop_loss')

        # 穿越语义降噪(借鉴 leek-fund):仅在价格"穿过"阈值或首次观测时提醒,
        # 价格停在阈值上/下方时不反复触发(仍保留 60min 去抖作安全网)。
        from alert_signals import crossed_up, crossed_down, entered_band
        prev = _PREV_PRICES.get(stock['id'])

        # 检查进场区间
        if entry_range and entry_range.get('min') and entry_range.get('max'):
            if current_price >= entry_range['min'] and current_price <= entry_range['max']:
                _fresh = entered_band(prev, current_price, entry_range['min'], entry_range['max']) or prev is None
                # 检查是否在最近60分钟内已发送过相同通知，避免重复
                if _fresh and not monitor_db.has_recent_notification(stock['id'], 'entry', minutes=60):
                    message = f"股票 {stock['symbol']} ({stock['name']}) 价格 {current_price} 进入进场区间 [{entry_range['min']}-{entry_range['max']}]"
                    monitor_db.add_notification(stock['id'], 'entry', message)
                    
                    # 立即发送通知（包括邮件）
                    notification_service.send_notifications()

        # 检查止盈
        if take_profit and current_price >= take_profit:
            _fresh = crossed_up(prev, current_price, take_profit) or prev is None
            # 检查是否在最近60分钟内已发送过相同通知，避免重复
            if _fresh and not monitor_db.has_recent_notification(stock['id'], 'take_profit', minutes=60):
                message = f"股票 {stock['symbol']} ({stock['name']}) 价格 {current_price} 达到止盈位 {take_profit}"
                monitor_db.add_notification(stock['id'], 'take_profit', message)
                
                # 立即发送通知（包括邮件）
                notification_service.send_notifications()

        # 检查止损
        if stop_loss and current_price <= stop_loss:
            _fresh = crossed_down(prev, current_price, stop_loss) or prev is None
            # 检查是否在最近60分钟内已发送过相同通知，避免重复
            if _fresh and not monitor_db.has_recent_notification(stock['id'], 'stop_loss', minutes=60):
                message = f"股票 {stock['symbol']} ({stock['name']}) 价格 {current_price} 达到止损位 {stop_loss}"
                monitor_db.add_notification(stock['id'], 'stop_loss', message)
                
                # 立即发送通知（包括邮件）
                notification_service.send_notifications()

        # 记录本轮价格,供下轮穿越判断
        _PREV_PRICES[stock['id']] = current_price

    def get_stocks_needing_update(self) -> List[Dict]:
        """获取需要更新价格的股票"""
        stocks = monitor_db.get_monitored_stocks()
        current_time = datetime.now()
        need_update = []
        
        for stock in stocks:
            last_checked = stock.get('last_checked')
            check_interval = stock.get('check_interval', 30)
            
            last_checked_dt = _coerce_naive_dt(last_checked)
            if not last_checked_dt:
                need_update.append(stock)
                continue

            next_check = last_checked_dt + timedelta(minutes=check_interval)
            if current_time >= next_check:
                need_update.append(stock)
        
        return need_update
    
    def manual_update_stock(self, stock_id: int):
        """手动更新股票价格"""
        stock = monitor_db.get_stock_by_id(stock_id)
        if stock:
            self._update_stock_price(stock)
            return True
        return False
    
    def get_scheduler(self):
        """获取调度器实例"""
        from monitor_scheduler import get_scheduler
        return get_scheduler(self)

# 全局监测服务实例
monitor_service = StockMonitorService()