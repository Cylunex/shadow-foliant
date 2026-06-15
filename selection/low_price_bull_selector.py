#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低价擒牛选股模块
使用pywencai获取低价高成长股票
"""

import pandas as pd
from datetime import datetime
from typing import Tuple, Optional
from data.pywencai_safe import pywencai_get
import time

# ⭐ _throttle 兼容(rate_limiter 可能在子进程中不可用)
try:
    from rate_limiter import throttle as _throttle
except Exception:
    def _throttle(*a, **k):
        return 0.0

# ⭐ 统一数据源配置
from selection.data_source_config import screen_stocks


class LowPriceBullSelector:
    """低价擒牛选股类"""
    
    def __init__(self):
        self.raw_data = None
        self.selected_stocks = None
    
    def get_low_price_stocks(self, top_n: int = 5) -> Tuple[bool, Optional[pd.DataFrame], str]:
        """
        获取低价高成长股票（数据源可切换）
        
        选股策略：
        - 股价<10元
        - 净利润增长率≥100%
        - 非ST
        - 沪深A股
        - 成交额由小至大排名
        
        数据源: 由 STRATEGY_DATA_SOURCE 控制 (auto/push2/pywencai)
        """
        try:
            print(f"\n{'='*60}")
            print(f"🐂 低价擒牛选股 - 数据获取中")
            print(f"{'='*60}")
            print(f"策略: 股价<10元 + 净利润增长率≥100% + 沪深A股")
            print(f"目标: 筛选前{top_n}只股票")
            
            # 优先用统一数据源
            result = screen_stocks(
                price_max=10,
                profit_growth_min=100,
                top_n=top_n,
                sort_field='f37',  # 成交额升序
                sort_asc=True,
            )
            
            if result['success'] and result['data']:
                df_result = pd.DataFrame(result['data'])
                df_result = df_result[df_result['price'].notna() & (df_result['price'] > 0)]
                df_result = df_result.sort_values('price')
                
                print(f"✅ {result['msg']}")
                print(f"\n✅ 选中的股票:")
                for idx, row in df_result.head(top_n).iterrows():
                    print(f"  {idx+1}. {row['code']} {row['name']} - 股价:{row['price']} PE:{row['pe']} 净利增长:{row['growth']}%")
                
                self.raw_data = df_result
                self.selected_stocks = df_result.head(top_n)
                return True, df_result.head(top_n), f"成功筛选出{min(top_n, len(df_result))}只"
            
            print(f"统一数据源: {result['msg']}，回退pywencai")
            
            # 回退: pywencai
            query = (
                "股价<10元，"
                "净利润增长率(净利润同比增长率)≥100%，"
                "非st，"
                "非科创板，"
                "非创业板，"
                "沪深A股，"
                "成交额由小至大排名"
            )
            print(f"\n查询语句: {query}")
            print(f"正在调用问财接口...")
            
            _throttle('pywencai')
            pywencai_result = pywencai_get(query, timeout=90)
            
            if pywencai_result is None:
                return False, None, "问财接口返回None，请检查网络或稍后重试"
            
            df_result = self._convert_to_dataframe(pywencai_result)
            
            if df_result is None or df_result.empty:
                return False, None, "未获取到符合条件的股票数据"
            
            print(f"✅ pywencai成功获取 {len(df_result)} 只股票")
            self.raw_data = df_result
            selected = df_result.head(top_n) if len(df_result) > top_n else df_result
            self.selected_stocks = selected
            
            print(f"\n✅ 选中的股票:")
            for idx, row in selected.head(top_n).iterrows():
                code = row.get('股票代码', 'N/A')
                name = row.get('股票简称', 'N/A')
                price = row.get('股价', row.get('最新价', 'N/A'))
                growth = row.get('净利润增长率', row.get('净利润同比增长率', 'N/A'))
                print(f"  {idx+1}. {code} {name} - 股价:{price} 净利增长:{growth}%")
            
            return True, selected, f"成功筛选出{len(selected)}只低价高成长股票"
            
        except Exception as e:
            error_msg = f"获取数据失败: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return False, None, error_msg
    
    def _convert_to_dataframe(self, result) -> Optional[pd.DataFrame]:
        """将pywencai返回结果转换为DataFrame"""
        try:
            if isinstance(result, pd.DataFrame):
                return result
            elif isinstance(result, dict):
                if 'data' in result:
                    return pd.DataFrame(result['data'])
                elif 'result' in result:
                    return pd.DataFrame(result['result'])
                else:
                    return pd.DataFrame(result)
            elif isinstance(result, list):
                return pd.DataFrame(result)
            else:
                print(f"⚠️ 未知的数据格式: {type(result)}")
                return None
        except Exception as e:
            print(f"转换DataFrame失败: {e}")
            return None
    
    def get_stock_codes(self) -> list:
        """
        获取选中股票的代码列表（去掉市场后缀）
        
        Returns:
            股票代码列表
        """
        if self.selected_stocks is None or self.selected_stocks.empty:
            return []
        
        codes = []
        for code in self.selected_stocks['股票代码'].tolist():
            if isinstance(code, str):
                # 去掉 .SZ 等后缀
                clean_code = code.split('.')[0] if '.' in code else code
                codes.append(clean_code)
            else:
                codes.append(str(code))
        
        return codes
