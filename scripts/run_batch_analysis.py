#!/usr/bin/env python3
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""批量股票分析CLI脚本 - Standalone模式"""
import sys
import os
import json
import time
import importlib.util

# 切到项目根(可移植:用 _bootstrap.ROOT,不再硬编码 ~/.openclaw 绝对路径)
os.chdir(_bootstrap.ROOT)

# 先加载环境变量
from dotenv import load_dotenv
load_dotenv(override=True)

import config
from data_source_manager import data_source_manager
from notification_service import NotificationService
import sqlite3

def get_portfolio_stocks():
    """从数据库获取持仓股票列表"""
    conn = sqlite3.connect(_bootstrap.db_path('portfolio_stocks.db'))
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT symbol, name FROM portfolio_stocks ORDER BY id')
        rows = cursor.fetchall()
        return [{'symbol': r[0], 'name': r[1]} for r in rows]
    except Exception as e:
        print(f"[ERROR] 读取持仓数据库失败: {e}")
        return []
    finally:
        conn.close()

def get_monitored_stocks():
    """从监控数据库获取监控股票列表"""
    conn = sqlite3.connect(_bootstrap.db_path('stock_monitor.db'))
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT symbol, name FROM monitored_stocks ORDER BY id')
        rows = cursor.fetchall()
        return [{'symbol': r[0], 'name': r[1]} for r in rows]
    except Exception as e:
        print(f"[ERROR] 读取监控数据库失败: {e}")
        return []
    finally:
        conn.close()

def fetch_stock_data(symbol, period='1y'):
    """获取股票数据（简化版，不含完整的AI分析）"""
    from stock_data import get_stock_data as app_get_stock_data
    
    print(f"\n{'='*50}")
    print(f"📊 分析 {symbol}...")
    
    try:
        # 方法1: 尝试使用app的get_stock_data
        stock_info, stock_data, indicators = app_get_stock_data(symbol, period)
        
        if "error" in stock_info or stock_data is None:
            raise Exception(stock_info.get('error', '数据获取失败'))
        
        # 兼容列名大小写（东财数据用 Close，akshare 初始化用 close）
        close_col = 'Close' if 'Close' in stock_data.columns else 'close'
        price = stock_data[close_col].iloc[-1] if len(stock_data) > 0 else 'N/A'
        print(f"  ✅ 成功获取数据: 最新价={price}, 共{len(stock_data)}条")
        
        return {
            'symbol': symbol,
            'success': True,
            'price': float(price) if price != 'N/A' else 0,
            'stock_info': stock_info,
            'data_points': len(stock_data),
            'indicators': indicators
        }
    except Exception as e:
        print(f"  ❌ 数据获取失败: {e}")
        return {'symbol': symbol, 'success': False, 'error': str(e)}

def send_webhook_notification(results):
    """发送批量分析结果通知"""
    try:
        from config_manager import ConfigManager
        cm = ConfigManager()
        notification_service = NotificationService()
        
        total = len(results)
        succeeded = sum(1 for r in results if r.get('success'))
        failed = total - succeeded
        
        # 构建通知
        success_stocks = [r for r in results if r.get('success')][:5]
        success_text = '\n'.join([
            f"- {r['symbol']}: 最新价{r.get('price', 'N/A')}"
            for r in success_stocks
        ])
        if len(success_stocks) < succeeded:
            success_text += f"\n  ...还有{succeeded - 5}只成功"
        
        failed_stocks = [r for r in results if not r.get('success')]
        failed_text = '\n'.join([
            f"- {r['symbol']}: {r.get('error', '未知错误')}"
            for r in failed_stocks[:5]
        ])
        
        msg = {
            "msgtype": "markdown",
            "markdown": {
                "title": "批量分析结果",
                "text": f"### 📊 批量分析完成\n\n"
                        f"**分析概况**\n"
                        f"- 总股票数: {total} 只\n"
                        f"- 成功: {succeeded} 只\n"
                        f"- 失败: {failed} 只\n\n"
                        f"**成功股票**\n{success_text or '无'}\n\n"
                        f"**失败股票**\n{failed_text or '无'}"
            }
        }
        
        import requests
        webhook_url = os.getenv('WEBHOOK_URL', 'http://127.0.0.1:18888/webhook/qq')
        resp = requests.post(webhook_url, json=msg, timeout=10)
        print(f"[通知] webhook响应: {resp.status_code}")
        return True
    except Exception as e:
        print(f"[通知] 发送失败: {e}")
        return False

def main():
    print(f"{'='*60}")
    print(f"🚀 批量股票数据分析CLI")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    # 获取股票列表
    portfolio = get_portfolio_stocks()
    monitored = get_monitored_stocks()
    
    print(f"\n持仓股票: {len(portfolio)} 只")
    print(f"监控股票: {len(monitored)} 只")
    
    # 优先使用持仓股票，如果为空则用监控股票
    if portfolio:
        stocks = portfolio
        print(f"使用持仓股票列表进行分析\n")
    elif monitored:
        stocks = [{'symbol': m['symbol'], 'name': m['name']} for m in monitored]
        print(f"使用监控股票列表进行分析\n")
    else:
        print("❌ 没有找到股票列表，退出")
        return
    
    # 分析每只股票
    results = []
    total = len(stocks)
    
    for i, stock in enumerate(stocks):
        symbol = stock['symbol']
        print(f"[{i+1}/{total}] ", end="")
        result = fetch_stock_data(symbol)
        results.append(result)
        
        # 避免太快请求
        if i < total - 1:
            time.sleep(0.5)
    
    # 输出汇总
    print(f"\n{'='*60}")
    print(f"📊 分析汇总")
    print(f"{'='*60}")
    
    succeeded = sum(1 for r in results if r.get('success'))
    failed = total - succeeded
    print(f"总计: {total} | ✅ 成功: {succeeded} | ❌ 失败: {failed}")
    
    # 发送通知
    print(f"\n{'='*60}")
    print(f"📤 发送通知...")
    send_webhook_notification(results)
    
    print(f"\n✅ 批量分析完成")
    print(f"结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == '__main__':
    main()
