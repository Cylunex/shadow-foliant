#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
净利增长选股模块
使用pywencai进行股票筛选
"""

import logging
from typing import Tuple, Optional
from selection.data_source_config import screen_stocks
import pandas as pd

# ⭐ _throttle 兼容(rate_limiter 可能在子进程中不可用)
try:
    from rate_limiter import throttle as _throttle
except Exception:
    def _throttle(*a, **k):
        return 0.0


class ProfitGrowthSelector:
    """净利增长选股器"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def get_profit_growth_stocks(self, top_n: int = 5) -> Tuple[bool, Optional[pd.DataFrame], str]:
        """
        获取符合净利增长策略的股票
        
        筛选条件：
        - 净利润增长率 ≥ 10%（净利润同比增长率）
        - 深圳A股
        - 非科创板
        - 非创业板
        - 非ST
        - 按成交额由小到大排名
        
        Args:
            top_n: 返回前N只股票
            
        Returns:
            (是否成功, 数据DataFrame, 消息)
        """
        try:
            from data.pywencai_safe import pywencai_get

            # 传真实净利增长门槛(≥10%,原来误传 None=零过滤,返回按ROE排的全市场前5污染推荐池)。
            # profit_growth_min 非空 → screen_stocks 能力守卫会跳过 push2/dataapi(clist 无净利增长
            # 字段)直接走问财;问财 query 完整编码策略条件,这里只是触发守卫的入口(2026-07-17 修)。
            self.logger.info('尝试统一数据源筛选...')
            unified = screen_stocks(
                price_max=None, pe_max=None, profit_growth_min=10,
                top_n=top_n, sort_field='f6', sort_asc=True,
            )
            if unified['success'] and unified['data']:
                df_result = pd.DataFrame(unified['data'])
                self.logger.info(f"✅ {unified['msg']}")
                return True, df_result, f"成功获取 {len(df_result)} 只股票"

            self.logger.warning(f"统一数据源失败({unified['msg']}), 回退pywencai")
            
            # 回退: pywencai
            query = (
                "净利润增长率(净利润同比增长率)≥10%，"
                "非科创板，"
                "非创业板，"
                "非ST，"
                "深圳A股，"
                "成交额由小至大排名"
            )
            
            self.logger.info(f"开始执行净利增长选股，查询条件: {query}")
            
            # 调用pywencai
            _throttle('pywencai')
            result = pywencai_get(query, timeout=90)
            
            if result is None or result.empty:
                self.logger.warning("未获取到符合条件的股票")
                return False, None, "未找到符合条件的股票"
            
            self.logger.info(f"获取到 {len(result)} 只股票")
            
            # 取前N只
            if len(result) > top_n:
                result = result.head(top_n)
                self.logger.info(f"筛选前 {top_n} 只股票")
            
            return True, result, f"成功获取 {len(result)} 只股票"
            
        except ImportError:
            error_msg = "pywencai模块未安装，请执行: pip install pywencai"
            self.logger.error(error_msg)
            return False, None, error_msg
            
        except Exception as e:
            error_msg = f"选股失败: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            traceback.print_exc()
            return False, None, error_msg
    
    def format_stock_info(self, df: pd.DataFrame) -> str:
        """
        格式化股票信息为文本
        
        Args:
            df: 股票数据DataFrame
            
        Returns:
            格式化后的文本
        """
        if df is None or df.empty:
            return "无数据"
        
        lines = []
        for idx, row in df.iterrows():
            stock_code = row.get('股票代码', 'N/A')
            stock_name = row.get('股票简称', 'N/A')
            profit_growth = row.get('净利润增长率', row.get('净利润同比增长率', 'N/A'))
            turnover = row.get('成交额', row.get('成交额[20241213]', 'N/A'))
            
            line = f"{idx+1}. {stock_code} {stock_name}"
            
            # 添加详细信息
            details = []
            
            if profit_growth != 'N/A':
                try:
                    details.append(f"净利增长:{float(profit_growth):.2f}%")
                except:
                    pass
            
            if turnover != 'N/A':
                try:
                    turnover_val = float(turnover)
                    if turnover_val >= 100000000:
                        details.append(f"成交额:{turnover_val/100000000:.2f}亿")
                    else:
                        details.append(f"成交额:{turnover_val/10000:.2f}万")
                except:
                    pass
            
            if details:
                line += f" - {', '.join(details)}"
            
            lines.append(line)
        
        return '\n'.join(lines)


# 全局实例
profit_growth_selector = ProfitGrowthSelector()
