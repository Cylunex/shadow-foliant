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
from selection.data_source_config import _normalize_wencai
import time

# ⭐ _throttle 兼容(rate_limiter 可能在子进程中不可用)
try:
    from rate_limiter import throttle as _throttle
except Exception:
    def _throttle(*a, **k):
        return 0.0

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
            
            # 该策略含净利增长条件，东财 push2/dataapi 无法表达。直接执行完整问句
            # 一次，避免统一入口失败后立刻重复打同一个问财源并触发全局熔断。
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
            pywencai_result = pywencai_get(query, timeout=60)
            
            if pywencai_result is None:
                return False, None, "问财接口返回None，请检查网络或稍后重试"
            
            df_result = self._convert_to_dataframe(pywencai_result)
            
            if df_result is None or df_result.empty:
                return False, None, "未获取到符合条件的股票数据"
            normalized = _normalize_wencai(df_result)
            if normalized is not None:
                df_result = normalized
            
            print(f"✅ pywencai成功获取 {len(df_result)} 只股票")
            self.raw_data = df_result
            selected = df_result.head(top_n) if len(df_result) > top_n else df_result
            self.selected_stocks = selected
            
            print(f"\n✅ 选中的股票:")
            for idx, row in selected.head(top_n).iterrows():
                code = row.get('股票代码', 'N/A')
                name = row.get('股票简称', 'N/A')
                price = row.get('股价', row.get('最新价', row.get('price', 'N/A')))
                growth = row.get(
                    '净利润增长率',
                    row.get('净利润同比增长率', row.get('growth', 'N/A')),
                )
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
