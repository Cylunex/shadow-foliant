#!/usr/bin/env python3
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""
持仓异动监测脚本 - 检查持仓股票涨跌幅超过2%的品种
在开市日 10:30/13:00/14:30 执行
"""
import sys
import os
import json
import time
import sqlite3
from datetime import datetime, timedelta

os.chdir(_bootstrap.ROOT)  # 可移植:切到项目根,不再硬编码 ~/.openclaw 绝对路径

from dotenv import load_dotenv
load_dotenv(override=True)

import config as _cfg
PROXY = _cfg.PROXY
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'http://127.0.0.1:18888/webhook/qq')
THRESHOLD = 2.0  # 涨跌幅阈值百分比

def is_trading_day():
    """判断今天是否是交易日（周一到周五）"""
    now = datetime.now()
    return now.weekday() < 5  # 0=周一, 4=周五

def get_portfolio_stocks():
    """从数据库获取持仓股票列表"""
    conn = sqlite3.connect(_bootstrap.db_path('portfolio_stocks.db'))
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT code, name, cost_price FROM portfolio_stocks ORDER BY id')
        return [{'symbol': r[0], 'name': r[1], 'cost': r[2]} for r in cursor.fetchall()]
    finally:
        conn.close()

def get_ticker(symbol):
    """根据代码确定yfinance ticker"""
    if symbol.startswith(('6', '5')):
        return f"{symbol}.SS"
    elif symbol.startswith(('0', '3')):
        return f"{symbol}.SZ"
    else:
        return f"{symbol}.SZ"

def fetch_price_data(symbol, db_name=None):
    """获取股票当前价格和前日收盘价"""
    try:
        import yfinance as yf
        
        old_http = os.environ.get('http_proxy')
        old_https = os.environ.get('https_proxy')
        os.environ['http_proxy'] = PROXY
        os.environ['https_proxy'] = PROXY
        
        ticker_symbol = get_ticker(symbol)
        ticker = yf.Ticker(ticker_symbol)
        
        # 获取最近5个交易日数据，用于计算涨跌幅
        hist = ticker.history(period='5d')
        
        if old_http is not None:
            os.environ['http_proxy'] = old_http
        else:
            os.environ.pop('http_proxy', None)
        if old_https is not None:
            os.environ['https_proxy'] = old_https
        else:
            os.environ.pop('https_proxy', None)
        
        if hist is None or hist.empty:
            return None
        
        # 最近的价格
        try:
            latest = hist.iloc[-1]
            current_price = round(float(latest['Close']), 3)
        except (IndexError, KeyError, TypeError):
            return None
        
        # 前收盘价（如果有2条以上数据，取倒数第二条的收盘）
        if len(hist) >= 2:
            try:
                prev_close = round(float(hist.iloc[-2]['Close']), 3)
            except (IndexError, KeyError, TypeError):
                prev_close = current_price
        else:
            prev_close = current_price  # 只有一条数据则用当前价
        
        change_pct = round((current_price - prev_close) / prev_close * 100, 2)
        
        # 优先使用数据库的中文名，其次yfinance
        name = db_name
        if not name or name == symbol:
            try:
                info = ticker.info or {}
                yf_name = info.get('shortName') or info.get('longName')
                if yf_name:
                    name = yf_name
            except:
                pass
        
        return {
            'symbol': symbol,
            'name': name or symbol,
            'current_price': current_price,
            'prev_close': prev_close,
            'change_pct': change_pct,
            'volume': int(latest['Volume']) if 'Volume' in latest else 0
        }
    except Exception as e:
        print(f"  ❌ {symbol}: {e}")
        return None

def send_qq_notification(content):
    """发送通知到QQ"""
    import requests
    msg = {
        "msgtype": "markdown",
        "markdown": {
            "title": "持仓异动提醒",
            "text": content
        }
    }
    try:
        r = requests.post(WEBHOOK_URL, json=msg, timeout=10)
        if r.status_code == 200:
            print(f"  ✅ 通知已发送")
        else:
            print(f"  ⚠️ 通知发送状态: {r.status_code}")
    except Exception as e:
        print(f"  ❌ 通知发送失败: {e}")

def main():
    now = datetime.now()
    beijing_time = now + timedelta(hours=8)
    time_str = beijing_time.strftime('%H:%M')
    
    print(f"⏰ [{now.strftime('%Y-%m-%d %H:%M:%S')} UTC] 持仓异动监测 (北京时间: {time_str})")
    
    # 交易日检查
    if not is_trading_day():
        print("  📅 非交易日，跳过")
        return
    
    # 获取持仓
    stocks = get_portfolio_stocks()
    if not stocks:
        print("  ❌ 没有持仓数据")
        return
    
    print(f"  📋 共 {len(stocks)} 只持仓股票")
    
    # 批量获取价格
    results = []
    for i, s in enumerate(stocks):
        print(f"  [{i+1}/{len(stocks)}] {s['symbol']} {s['name']}...", end=" ")
        sys.stdout.flush()
        data = fetch_price_data(s['symbol'], db_name=s['name'])
        if data:
            print(f"¥{data['current_price']} ({data['change_pct']:+.2f}%)")
            results.append(data)
        else:
            print("❌")
        time.sleep(0.8)  # 避免请求过快
    
    # 筛选异动股票（涨跌幅超过阈值）
    movers = [r for r in results if abs(r['change_pct']) >= THRESHOLD]
    
    # 构建通知内容
    lines = []
    data_date = beijing_time.strftime('%Y-%m-%d')
    
    if movers:
        # 按涨跌幅排序
        movers.sort(key=lambda x: abs(x['change_pct']), reverse=True)
        
        gainers = [m for m in movers if m['change_pct'] > 0]
        losers = [m for m in movers if m['change_pct'] < 0]
        
        lines.append(f"### 📊 持仓异动提醒 ({data_date} {time_str})\n")
        
        if gainers:
            lines.append(f"**📈 涨幅超{THRESHOLD}% ({len(gainers)})**")
            for m in gainers:
                emoji = "🔴" if m['change_pct'] > 5 else "🔺"
                lines.append(f"{emoji} {m['symbol']} {m['name']}: **+{m['change_pct']:.2f}%** (¥{m['current_price']})")
            lines.append("")
        
        if losers:
            lines.append(f"**📉 跌幅超{THRESHOLD}% ({len(losers)})**")
            for m in losers:
                lines.append(f"🟢 {m['symbol']} {m['name']}: **{m['change_pct']:.2f}%** (¥{m['current_price']})")
            lines.append("")
    else:
        lines.append(f"### 📊 持仓监测 ({data_date} {time_str})\n")
        lines.append(f"✅ 暂无股票涨跌幅超过{THRESHOLD}%\n")
    
    content = '\n'.join(lines)
    
    # 如果内容太长，精简
    if len(content) > 1500:
        # 只保留异动部分
        lines = lines[:lines.index('')+1] if '' in lines else lines[:3]
        lines.append(f"\n💡 详情请查看App")
        content = '\n'.join(lines)
    
    print(f"\n📤 发送通知 ({len(movers)} 只异动, {len(content)}字符)...")
    send_qq_notification(content)
    print(f"✅ 完成")

if __name__ == '__main__':
    main()
