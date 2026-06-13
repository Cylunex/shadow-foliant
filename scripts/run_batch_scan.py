#!/usr/bin/env python3
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""批量股票分析 - 后台运行脚本"""
import sys
import os
import json
import time
import requests

os.chdir(_bootstrap.ROOT)  # 可移植:切到项目根,不再硬编码 ~/.openclaw 绝对路径

from dotenv import load_dotenv
load_dotenv(override=True)

import sqlite3

def get_stock_list():
    """获取股票列表"""
    conn = sqlite3.connect(_bootstrap.db_path('portfolio_stocks.db'))
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT code, name FROM portfolio_stocks ORDER BY id')
        rows = cursor.fetchall()
        return [{'symbol': r[0], 'name': r[1]} for r in rows]
    except Exception as e:
        print(f"[DB] 读取持仓表失败: {e}")
        return []
    finally:
        conn.close()

def get_yfinance_ticker(symbol):
    """根据股票代码判断市场后缀"""
    if symbol.startswith(('6', '5')):
        return f"{symbol}.SS"
    elif symbol.startswith(('0', '3')):
        return f"{symbol}.SZ"
    else:
        # ETF等特殊代码，尝试多个后缀
        return f"{symbol}.SZ"

def fetch_price(symbol):
    """获取最新价格 - 优先yfinance"""
    try:
        import yfinance as yf
        import config as _cfg
        proxy = _cfg.PROXY
        
        old_http = os.environ.get('http_proxy')
        old_https = os.environ.get('https_proxy')
        os.environ['http_proxy'] = proxy
        os.environ['https_proxy'] = proxy
        
        # 根据代码判断市场
        tickers_to_try = [get_yfinance_ticker(symbol)]
        # 再试一下不加后缀
        tickers_to_try.append(symbol)
        # 也试一下反向后缀
        if symbol.startswith(('6', '5')):
            tickers_to_try.append(f"{symbol}.SZ")
        else:
            tickers_to_try.append(f"{symbol}.SS")
        
        price = None
        name = None
        
        for ts in tickers_to_try:
            try:
                ticker = yf.Ticker(ts)
                hist = ticker.history(period='1mo')
                if hist is not None and not hist.empty:
                    price = round(float(hist['Close'].iloc[-1]), 3)
                    info = ticker.info or {}
                    name = info.get('shortName') or info.get('longName')
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
        
        return price, name
    except Exception as e:
        return None, None

def send_notification(content):
    """发送通知到QQ"""
    webhook_url = os.getenv('WEBHOOK_URL', 'http://127.0.0.1:18888/webhook/qq')
    msg = {
        "msgtype": "markdown",
        "markdown": {
            "title": "批量分析结果",
            "text": content
        }
    }
    try:
        r = requests.post(webhook_url, json=msg, timeout=10)
        return r.status_code == 200
    except:
        return False

def main():
    print(f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("📊 开始批量查询持仓行情...")
    
    stocks = get_stock_list()
    if not stocks:
        print("❌ 没有找到股票")
        send_notification("### ❌ 批量扫描失败\n\n没有找到持仓股票列表")
        return
    
        print("❌ 没有找到股票")
        send_notification("### ❌ 批量分析失败\n没有找到持仓股票列表")
        return
    
    print(f"📋 共 {len(stocks)} 只股票")
    
    results = []
    for i, s in enumerate(stocks):
        sym = s['symbol']
        print(f"  [{i+1}/{len(stocks)}] {sym} ({s['name']})...", end=" ")
        sys.stdout.flush()
        
        price, name = fetch_price(sym)
        if price:
            print(f"¥{price}")
            results.append({'symbol': sym, 'name': name or s['name'], 'price': price, 'success': True})
        else:
            print("❌")
            results.append({'symbol': sym, 'name': s['name'], 'price': '--', 'success': False})
        
        time.sleep(0.5)
    
    # 汇总
    succeeded = [r for r in results if r.get('success')]
    failed = [r for r in results if not r.get('success')]
    
    # 构建通知内容
    lines = []
    lines.append("### 📊 持仓批量行情扫描\n")
    lines.append(f"**查询时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**总计**: {len(stocks)} 只 | ✅ {len(succeeded)} | ❌ {len(failed)}\n")
    
    lines.append("**持仓行情**")
    for r in succeeded:
        lines.append(f"- {r['symbol']} {r['name']}: **¥{r['price']}**")
    
    if failed:
        lines.append(f"\n**查询失败**")
        for r in failed:
            lines.append(f"- {r['symbol']} {r['name']}")
    
    content = '\n'.join(lines)
    print(f"\n📤 发送通知...")
    ok = send_notification(content)
    print(f"{'✅ 通知已发送' if ok else '❌ 通知发送失败'}")
    print(f"\n✅ 完成")

if __name__ == '__main__':
    main()
