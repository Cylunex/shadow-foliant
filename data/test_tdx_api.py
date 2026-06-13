#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TDX API配置测试脚本
用于测试TDX API连接和数据获取是否正常
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Guard：避免作为模块被其他文件 import 时执行真实网络测试
# 此脚本只能从命令行直接运行（python test_tdx_api.py）
if __name__ != '__main__':
    raise ImportError(
        'test_tdx_api.py 是命令行测试脚本，不应被 import。'
        '直接运行: python test_tdx_api.py'
    )

# 加载环境变量
load_dotenv()

# 获取TDX API URL
TDX_API_URL = os.getenv('TDX_BASE_URL', 'http://127.0.0.1:5000')

print("=" * 60)
print("TDX API配置测试")
print("=" * 60)
print(f"\n1. TDX API地址: {TDX_API_URL}")

# 测试1: 健康检查
print("\n2. 测试健康检查接口...")
try:
    response = requests.get(f"{TDX_API_URL}/api/health", timeout=5)
    if response.status_code == 200:
        print("   ✅ 健康检查成功")
        print(f"   响应: {response.text}")
    else:
        print(f"   ❌ 健康检查失败: HTTP {response.status_code}")
        sys.exit(1)
except Exception as e:
    print(f"   ❌ 连接失败: {e}")
    print("\n提示:")
    print("   - 请检查TDX API服务是否已启动")
    print("   - 请检查.env中的TDX_API_URL配置是否正确")
    print("   - 默认地址: http://127.0.0.1:8181")
    sys.exit(1)

# 测试2: 获取K线数据
print("\n3. 测试K线数据接口...")

# 尝试不同的代码格式
test_codes = [
    ("SZ000001", "平安银行"),
    ("000001", "平安银行(纯数字)"),
    ("SH600000", "浦发银行"),
    ("600000", "浦发银行(纯数字)"),
]

data = None
test_code = None

for code, name in test_codes:
    print(f"\n   尝试股票: {code} ({name})")
    
    try:
        url = f"{TDX_API_URL}/api/kline"
        params = {
            'code': code,
            'type': 'day'
        }
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # 支持两种数据格式
            kline_list = None
            if isinstance(data, dict) and 'data' in data:
                # 嵌套格式: {code: 0, message: "success", data: {List: [...]}}
                if data.get('code') == 0:
                    data_obj = data.get('data', {})
                    kline_list = data_obj.get('List', [])
            elif isinstance(data, list):
                # 直接数组格式
                kline_list = data
            
            if kline_list and len(kline_list) > 0:
                test_code = code
                data = kline_list  # 保存为全局变量
                print(f"   ✅ K线数据获取成功！")
                print(f"   数据条数: {len(kline_list)}")
                break
            else:
                print(f"   ⚠️ 数据为空")
        else:
            print(f"   ❌ HTTP {response.status_code}")
    except Exception as e:
        print(f"   ❌ 错误: {e}")

if data is None or test_code is None:
    print(f"\n   ❌ 所有代码格式都失败，无法继续测试")
    print("\n提示：")
    print("   - 请检查TDX API服务是否正确启动")
    print("   - 请确认API支持的股票代码格式")
    print("   - 可能的格式：SZ000001, 000001, SH600000, 600000")
    sys.exit(1)

print(f"\n   成功的代码格式: {test_code}")

# 显示最新一条数据
if len(data) > 0:
    latest = data[-1]
    print(f"\n   最新K线数据:")
    # 支持两种字段名格式：小写和大写
    print(f"   - 日期: {latest.get('date') or latest.get('Time', 'N/A')}")
    print(f"   - 开盘: {latest.get('open') or latest.get('Open', 'N/A')}")
    print(f"   - 收盘: {latest.get('close') or latest.get('Close', 'N/A')}")
    print(f"   - 最高: {latest.get('high') or latest.get('High', 'N/A')}")
    print(f"   - 最低: {latest.get('low') or latest.get('Low', 'N/A')}")
    print(f"   - 成交量: {latest.get('volume') or latest.get('Volume', 'N/A')}")

# 检查数据量是否足够计算MA20
if len(data) >= 20:
    print(f"   ✅ 数据量充足，可以计算MA20（需要至少20条）")
else:
    print(f"   ⚠️ 数据量不足，仅{len(data)}条，需要至少20条才能计算MA20")
    print(f"   请尝试其他股票或等待数据积累")

# 测试3: 计算均线
print("\n4. 测试均线计算...")
try:
    import pandas as pd
    
    df = pd.DataFrame(data)
    
    # 支持两种字段名：小写close和大写Close
    if 'Close' in df.columns and 'close' not in df.columns:
        df['close'] = df['Close']
    
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    
    # 计算MA5和MA20
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA20'] = df['close'].rolling(window=20).mean()
    
    latest = df.iloc[-1]
    
    if pd.notna(latest['MA5']) and pd.notna(latest['MA20']):
        print(f"   ✅ 均线计算成功")
        print(f"   - 收盘价: {latest['close']:.2f}")
        print(f"   - MA5: {latest['MA5']:.2f}")
        print(f"   - MA20: {latest['MA20']:.2f}")
        
        # 判断MA5和MA20的关系
        if latest['MA5'] > latest['MA20']:
            print(f"   - 趋势: 🟢 MA5 > MA20 (多头)")
        elif latest['MA5'] < latest['MA20']:
            print(f"   - 趋势: 🔴 MA5 < MA20 (空头)")
        else:
            print(f"   - 趋势: 🟡 MA5 = MA20 (震荡)")
    else:
        print(f"   ❌ 均线计算失败，数据包含NaN")
        sys.exit(1)
        
except Exception as e:
    print(f"   ❌ 均线计算失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 所有测试通过
print("\n" + "=" * 60)
print("✅ 所有测试通过！TDX API配置正常")
print("=" * 60)
print("\n提示:")
print("   - 现在可以启动低价擒牛策略监控服务")
print("   - 在监控面板中点击'▶️ 启动监控服务'")
print("   - 服务将每60秒扫描一次监控列表中的股票")
print("")
