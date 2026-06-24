#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低价擒牛策略监控服务
定时扫描股票，检测卖出信号
"""

import time
import threading
import logging
from datetime import datetime
from typing import Optional
import os

from low_price_bull_monitor import low_price_bull_monitor
from notification_service import notification_service


class LowPriceBullService:
    """低价擒牛策略监控服务"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.scan_interval = 60  # 默认扫描间隔（秒）
        self.holding_days_limit = 5  # 持股天数限制
        
        # 从环境变量读取配置
        self._load_config()
    
    def _load_config(self):
        """从环境变量加载配置"""
        try:
            from dotenv import load_dotenv
            load_dotenv()
            
            # 扫描间隔
            interval = os.getenv('LOW_PRICE_BULL_SCAN_INTERVAL', '60')
            self.scan_interval = int(interval)
            
            # 持股天数限制
            days = os.getenv('LOW_PRICE_BULL_HOLDING_DAYS', '5')
            self.holding_days_limit = int(days)
            
            # TDX API配置
            self.tdx_api_url = os.getenv('TDX_BASE_URL', 'http://127.0.0.1:5000')
            
            self.logger.info(f"监控配置: 扫描间隔={self.scan_interval}秒, 持股天数限制={self.holding_days_limit}天")
            self.logger.info(f"TDX API: {self.tdx_api_url}")
            
        except Exception as e:
            self.logger.warning(f"加载配置失败，使用默认值: {e}")
    
    def start(self):
        """启动监控服务"""
        if self.running:
            self.logger.warning("监控服务已在运行")
            return False
        
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        
        self.logger.info("低价擒牛监控服务已启动")
        return True
    
    def stop(self):
        """停止监控服务"""
        if not self.running:
            return False
        
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        
        self.logger.info("低价擒牛监控服务已停止")
        return True
    
    def _monitor_loop(self):
        """监控循环"""
        while self.running:
            try:
                self._scan_stocks()
                time.sleep(self.scan_interval)
            except Exception as e:
                self.logger.error(f"监控循环错误: {e}")
                time.sleep(self.scan_interval)
    
    def _scan_stocks(self):
        """扫描所有监控的股票"""
        try:
            # 更新持有天数
            low_price_bull_monitor.update_holding_days()
            
            # 获取监控列表
            stocks = low_price_bull_monitor.get_monitored_stocks()
            
            if not stocks:
                return
            
            self.logger.info(f"开始扫描 {len(stocks)} 只股票")
            
            for stock in stocks:
                try:
                    self._check_stock(stock)
                except Exception as e:
                    self.logger.error(f"检查股票 {stock['stock_code']} 失败: {e}")
            
            # 处理提醒
            self._process_alerts()
            
        except Exception as e:
            self.logger.error(f"扫描股票失败: {e}")
    
    def _check_stock(self, stock: dict):
        """
        检查单只股票的卖出信号
        
        Args:
            stock: 股票信息字典
        """
        stock_code = stock['stock_code']
        stock_name = stock['stock_name']
        holding_days = stock['holding_days']
        
        # 检查1: 持股天数
        if holding_days >= self.holding_days_limit:
            # 添加提醒
            low_price_bull_monitor.add_sell_alert(
                stock_code=stock_code,
                stock_name=stock_name,
                alert_type='holding_days',
                alert_reason=f'持股满{self.holding_days_limit}天，建议卖出',
                holding_days=holding_days
            )
            self.logger.info(f"{stock_code} 持股满{self.holding_days_limit}天，生成卖出提醒")
            return
        
        # 检查2: MA5下穿MA20
        current_price, ma5, ma20 = self._get_stock_data(stock_code)
        
        if current_price and ma5 and ma20:
            if ma5 < ma20:
                # MA5下穿MA20，添加提醒
                low_price_bull_monitor.add_sell_alert(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    alert_type='ma_cross',
                    alert_reason='MA5下穿MA20，技术信号卖出',
                    current_price=current_price,
                    ma5=ma5,
                    ma20=ma20,
                    holding_days=holding_days
                )
                self.logger.info(f"{stock_code} MA5下穿MA20，生成卖出提醒")
    
    def _get_stock_data(self, stock_code: str) -> tuple:
        """获取 (当前价, MA5, MA20)。
        ⭐ 2026-06-24 改走 datahub 统一数据层(不再依赖内网自建 TDX HTTP 服务):
          - MA5/MA20:前复权 qfq(避除权跳空毁均线下穿信号)
          - 当前价:raw 真实价(实时报价;挂了回退 qfq 最新)
        失败返回 (None, None, None)。"""
        try:
            import datahub
            import pandas as pd
            clean_code = stock_code.split('.')[0] if '.' in stock_code else stock_code
            # MA5/MA20 用前复权 3mo(≈60交易日,足够算 MA20)
            df = datahub.kline(clean_code, '3mo', adjust='qfq')
            if df is None or getattr(df, 'empty', True) or len(df) < 20:
                self.logger.warning(f"{stock_code} K线数据不足(需≥20天)")
                return None, None, None
            close_col = 'Close' if 'Close' in df.columns else 'close'
            closes = pd.to_numeric(df[close_col], errors='coerce')
            ma5 = float(closes.rolling(window=5).mean().iloc[-1])
            ma20 = float(closes.rolling(window=20).mean().iloc[-1])
            # 当前价:实时 raw 真实价(MA 信号是 qfq,但展示/记录的现价用真实价)
            current_price = None
            try:
                q = datahub.quote(clean_code)
                if isinstance(q, dict) and q.get('price'):
                    current_price = float(q['price'])
            except Exception:
                pass
            if current_price is None:
                current_price = float(closes.iloc[-1])   # 回退 qfq 最新(最新段≈raw)
            if pd.isna(ma5) or pd.isna(ma20) or current_price is None:
                self.logger.warning(f"{stock_code} 数据含 NaN")
                return None, None, None
            self.logger.info(f"{stock_code} 价格={current_price:.2f} MA5={ma5:.2f}(qfq) MA20={ma20:.2f}(qfq)")
            return current_price, ma5, ma20
        except Exception as e:
            self.logger.error(f"获取股票数据失败 {stock_code}: {e}")
            return None, None, None
    
    def _process_alerts(self):
        """处理待发送的提醒"""
        try:
            alerts = low_price_bull_monitor.get_pending_alerts()
            
            if not alerts:
                return
            
            self.logger.info(f"处理 {len(alerts)} 条卖出提醒")
            
            for alert in alerts:
                try:
                    # 发送通知
                    self._send_alert_notification(alert)
                    
                    # 标记已发送
                    low_price_bull_monitor.mark_alert_sent(alert['id'])
                    
                    # 自动移除股票
                    low_price_bull_monitor.remove_stock(
                        alert['stock_code'],
                        reason=alert['alert_reason']
                    )
                    
                    self.logger.info(f"已处理提醒并移除股票: {alert['stock_code']}")
                    
                except Exception as e:
                    self.logger.error(f"处理提醒失败: {e}")
        
        except Exception as e:
            self.logger.error(f"处理提醒失败: {e}")
    
    def _send_alert_notification(self, alert: dict):
        """
        发送卖出提醒通知
        
        Args:
            alert: 提醒信息字典
        """
        try:
            # 构建消息
            keyword = notification_service.config.get('webhook_keyword', 'aiagents通知')
            
            message_text = f"### {keyword} - 低价擒牛卖出提醒\n\n"
            message_text += f"**股票代码**: {alert['stock_code']}\n\n"
            message_text += f"**股票名称**: {alert['stock_name']}\n\n"
            message_text += f"**提醒类型**: {self._get_alert_type_name(alert['alert_type'])}\n\n"
            message_text += f"**提醒原因**: {alert['alert_reason']}\n\n"
            
            # 添加详细信息（确保数据类型正确）
            current_price = alert.get('current_price')
            if current_price is not None:
                try:
                    price_val = float(current_price)
                    message_text += f"**当前价格**: {price_val:.2f}元\n\n"
                except (ValueError, TypeError):
                    pass
            
            ma5 = alert.get('ma5')
            ma20 = alert.get('ma20')
            if ma5 is not None and ma20 is not None:
                try:
                    ma5_val = float(ma5)
                    ma20_val = float(ma20)
                    message_text += f"**MA5**: {ma5_val:.2f}\n\n"
                    message_text += f"**MA20**: {ma20_val:.2f}\n\n"
                except (ValueError, TypeError):
                    pass
            
            holding_days = alert.get('holding_days')
            if holding_days is not None:
                try:
                    days_val = int(holding_days)
                    message_text += f"**持有天数**: {days_val}天\n\n"
                except (ValueError, TypeError):
                    pass
            
            message_text += f"**提醒时间**: {alert['alert_time']}\n\n"
            message_text += "---\n\n"
            message_text += "**建议**: 开盘时卖出该股票\n\n"
            message_text += "_此消息由AI股票分析系统自动发送_"
            
            # 发送钉钉通知
            if notification_service.config['webhook_enabled']:
                import requests
                
                data = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": f"{keyword} - 卖出提醒",
                        "text": message_text
                    }
                }
                
                response = requests.post(
                    notification_service.config['webhook_url'],
                    json=data,
                    headers={'Content-Type': 'application/json'},
                    timeout=10
                )
                
                if response.status_code == 200:
                    self.logger.info(f"卖出提醒已发送: {alert['stock_code']}")
                else:
                    self.logger.error(f"发送提醒失败: HTTP {response.status_code}")
            
        except Exception as e:
            self.logger.error(f"发送通知失败: {e}")
    
    def _get_alert_type_name(self, alert_type: str) -> str:
        """获取提醒类型名称"""
        type_map = {
            'holding_days': '持股到期',
            'ma_cross': 'MA均线死叉'
        }
        return type_map.get(alert_type, alert_type)
    
    def get_status(self) -> dict:
        """获取服务状态"""
        stocks = low_price_bull_monitor.get_monitored_stocks()
        alerts = low_price_bull_monitor.get_pending_alerts()
        
        return {
            'running': self.running,
            'scan_interval': self.scan_interval,
            'holding_days_limit': self.holding_days_limit,
            'monitored_count': len(stocks),
            'pending_alerts': len(alerts)
        }


# 全局服务实例
low_price_bull_service = LowPriceBullService()
