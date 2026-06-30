#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主力选股模块
使用pywencai获取主力资金净流入前100名股票，并进行智能筛选
"""

from numpy.ma import minimum_fill_value
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import time
import os
import pickle

# ⭐ 全项目统一从 data.pywencai_safe 调 pywencai, 自带硬超时(防僵尸进程)
from data.pywencai_safe import pywencai_get

# ⭐ _throttle 兼容(rate_limiter 可能在子进程中不可用)
try:
    from rate_limiter import throttle as _throttle
except Exception:
    def _throttle(*a, **k):
        return 0.0


# ── 主力选股当日缓存(2026-06-26)──────────────────────────────────────────
# 问财"主力资金净流入排名"是全市场单次查询、日级 EOD 口径(已完成的天收盘即定数)。
# 盘前 09:15 预取写当日缓存 → 09:45 综合选股的主力资金策略读缓存,不在选股高峰现调问财
# (问财熔断/卡死时主力选股会退化成"按市值选股")。缓存键=当日+days_ago+市值档,跨交易日自然失效。
_MF_CACHE_DIR = None


def _mf_cache_dir() -> str:
    global _MF_CACHE_DIR
    if _MF_CACHE_DIR is None:
        try:
            import _bootstrap
            d = _bootstrap.db_path('main_force_cache')
        except Exception:
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'db', 'main_force_cache')
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        _MF_CACHE_DIR = d
    return _MF_CACHE_DIR


def _mf_cache_key(days_ago, min_mcap, max_mcap) -> str:
    today = datetime.now().strftime('%Y-%m-%d')
    return f"mf_{today}_d{days_ago}_{int(min_mcap or 0)}_{int(max_mcap or 0)}"


def _mf_cache_load(key: str):
    try:
        p = os.path.join(_mf_cache_dir(), key + '.pkl')
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return pickle.load(f)
    except Exception:
        pass
    return None


def _mf_cache_save(key: str, df) -> None:
    try:
        with open(os.path.join(_mf_cache_dir(), key + '.pkl'), 'wb') as f:
            pickle.dump(df, f)
    except Exception:
        pass


def _fetch_via_akshare_fund_flow(min_market_cap: float = None,
                                 max_market_cap: float = None,
                                 top_n: int = 100):
    """**带主力资金数据的兜底源**(2026-06-22): pywencai 卡死时, 走 akshare
    stock_individual_fund_flow_rank 拉今日资金流排名, 字段对齐成 pywencai 同款
    中文列, 让后续 _convert_to_dataframe / get_top_stocks 零改动可用。

    akshare 此接口:
      - 一次返回全市场 A 股(~5500 只)的资金流(代码/名称/最新价/今日涨跌幅/
        主力净流入-净额/主力净流入-净占比/超大单/大单/中单/小单)
      - 走 akshare_safe.call 30s 硬超时(不会卡死)
      - 跟 pywencai 同源(都是东财数据), 但纯 HTTP+JSON, 不走 JS exec, 远稳定

    返回 DataFrame(同 pywencai 格式) 或 None。
    """
    try:
        import akshare as ak
        from data.akshare_safe import call as ak_call
    except Exception as e:
        print(f"  ❌ akshare 资金流兜底不可用: {type(e).__name__}: {e}")
        return None
    try:
        _throttle('akshare')
        # 兼容老版本 akshare 不接受 market 参数
        try:
            df = ak_call(ak.stock_individual_fund_flow_rank, timeout=30, indicator='今日')
        except TypeError:
            df = ak_call(ak.stock_individual_fund_flow_rank, timeout=30)
    except Exception as e:
        print(f"  ❌ akshare 资金流兜底失败: {type(e).__name__}: {str(e)[:120]}")
        return None
    if df is None or df.empty:
        print('  ❌ akshare 资金流兜底返回空')
        return None
    # 字段对齐:akshare 列名 → pywencai 风格中文列 (后续 _convert_to_dataframe 不用改)
    # akshare 字段: 代码/名称/最新价/今日涨跌幅/今日主力净流入-净额/...
    rename = {
        '代码': '股票代码', '名称': '股票简称',
        '最新价': '最新价', '今日涨跌幅': '区间涨跌幅',
        '今日主力净流入-净额': '主力资金流向',
        '今日主力净流入-净占比': '主力资金流向占比',
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if '股票代码' not in df.columns or '主力资金流向' not in df.columns:
        print(f'  ❌ akshare 资金流字段异常: {list(df.columns)[:10]}')
        return None
    # 主力净额降序 → 取前 top_n
    df = df.copy()
    df['主力资金流向'] = pd.to_numeric(df['主力资金流向'], errors='coerce').fillna(0)
    df = df.sort_values('主力资金流向', ascending=False).head(top_n).reset_index(drop=True)
    print(f'  ✅ akshare 资金流兜底成功: 取主力净流入前 {len(df)} 只')
    return df

class MainForceStockSelector:
    """主力选股类"""
    
    def __init__(self):
        self.raw_data = None
        self.filtered_stocks = None
    
    def get_main_force_stocks(self, start_date: str = None, days_ago: int = None,
                             min_market_cap: float = None, max_market_cap: float = None) -> Tuple[bool, pd.DataFrame, str]:
        """
        获取主力资金净流入前100名股票
        
        Args:
            start_date: 开始日期，格式如"2025年10月1日"，如果不提供则使用days_ago
            days_ago: 距今多少天
            min_market_cap: 最小市值限制
            max_market_cap: 最大市值限制
            
        Returns:
            (success, dataframe, message)
        """
        try:
            # 如果没有提供开始日期，根据days_ago计算
            if not start_date:
                if days_ago is None:
                    days_ago = 30  # 默认追溯 30 天
                date_obj = datetime.now() - timedelta(days=days_ago)
                start_date = f"{date_obj.year}年{date_obj.month}月{date_obj.day}日"
            # 市值参数兜底
            if min_market_cap is None:
                min_market_cap = 50
            if max_market_cap is None:
                max_market_cap = 5000
            
            print(f"\n{'='*60}")
            print(f"🔍 主力选股 - 数据获取中")
            print(f"{'='*60}")
            print(f"开始日期: {start_date}")
            print(f"目标: 获取主力资金净流入排名前100名股票")
            
            # 构建查询语句 - 使用多个备选方案，所有方案都要求计算区间涨跌幅。
            # ⚠️ 2026-06-30 删原方案1(8个能力评分字段太长,问财每次返 None 白费 5s)。
            # 当前方案1=原方案2(简化查询,实测稳定);方案2=原方案3;方案3=原方案4。
            queries = [
                # 方案1: 简化查询(原方案2,实测稳定一次过)
                f"{start_date}以来主力资金净流入，并计算区间涨跌幅，市值{min_market_cap}-{max_market_cap}亿，非科创非st，"
                f"所属同花顺行业，总市值，净利润，营收，市盈率，市净率",

                # 方案2: 基础查询(原方案3)
                f"{start_date}以来主力资金净流入排名，并计算区间涨跌幅，市值{min_market_cap}-{max_market_cap}亿，非科创非st，"
                f"所属行业，总市值",

                # 方案3: 最简查询(原方案4)
                f"{start_date}以来主力资金净流入前100名，并计算区间涨跌幅，市值{min_market_cap}-{max_market_cap}亿，非st非科创板，所属行业，总市值",
            ]
            
            # 尝试不同的查询方案（最多试2个pywencai查询，失败就直接降级）
            for i, query in enumerate(queries[:2], 1):
                print(f"\n尝试方案 {i}/{len(queries)}...")
                print(f"查询语句: {query[:100]}...")
                
                try:
                    _throttle('pywencai')
                    result = pywencai_get(query, timeout=90)

                    if result is None:
                        print(f"  ⚠️ 方案{i}返回None，尝试下一个方案")
                        continue
                    
                    # 转换为DataFrame
                    df_result = self._convert_to_dataframe(result)
                    
                    if df_result is None or df_result.empty:
                        print(f"  ⚠️ 方案{i}数据为空，尝试下一个方案")
                        continue
                    
                    # 成功获取数据
                    print(f"  ✅ 方案{i}成功！获取到 {len(df_result)} 只股票")
                    self.raw_data = df_result
                    
                    # 显示获取到的列名
                    print(f"\n获取到的数据字段:")
                    for col in df_result.columns[:15]:  # 只显示前15个字段
                        print(f"  - {col}")
                    if len(df_result.columns) > 15:
                        print(f"  ... 还有 {len(df_result.columns) - 15} 个字段")
                    
                    return True, df_result, f"成功获取{len(df_result)}只股票数据"
                
                except Exception as e:
                    print(f"  ❌ 方案{i}失败: {str(e)}")
                    time.sleep(2)  # 失败后稍等再试，避免触发反爬
            
            # 所有 pywencai 方案都失败 → 走兜底链
            error_msg = "所有pywencai查询方案都失败，启动兜底链"
            print(f"\n⚠️ {error_msg}")
            fallback_df = None

            # ⭐ 第 0 层: akshare stock_individual_fund_flow_rank (带主力资金数据!)
            # 2026-06-22 新增: 之前 pywencai 卡死后只能降级到 push2/dataapi, 但那两个没主力
            # 资金字段, 主力选股变成"按市值选股"。akshare 同样东财源头但纯 HTTP, 稳定。
            try:
                ak_df = _fetch_via_akshare_fund_flow(
                    min_market_cap=min_market_cap, max_market_cap=max_market_cap, top_n=100)
                if ak_df is not None and not ak_df.empty:
                    fallback_df = ak_df
                    self.raw_data = fallback_df
                    return True, fallback_df, f'兜底成功(akshare 资金流): {len(fallback_df)} 只(含主力数据)'
            except Exception as fe:
                print(f"  ❌ akshare 资金流降级失败: {fe}")

            # 第1层: push2 (无主力资金数据, 仅按市值/价格)
            try:
                from selection.data_source_config import fetch_stocks_push2
                fallback = fetch_stocks_push2(mcap_min=min_market_cap, mcap_max=max_market_cap, top_n=100)
                if fallback.get('success') and fallback.get('data'):
                    import pandas as pd
                    fallback_df = pd.DataFrame(fallback['data'])
                    fallback_df.rename(columns={'code': '股票代码', 'name': '股票简称', 'pe': '市盈率', 'mcap': '总市值'}, inplace=True)
            except Exception as fe:
                print(f"  ❌ push2降级失败: {fe}")
            # 第2层: dataapi
            if fallback_df is None or fallback_df.empty:
                try:
                    from selection.data_source_config import fetch_stocks_dataapi
                    fallback = fetch_stocks_dataapi(mcap_min=min_market_cap, mcap_max=max_market_cap, top_n=100)
                    if fallback.get('success') and fallback.get('data'):
                        import pandas as pd
                        fallback_df = pd.DataFrame(fallback['data'])
                        fallback_df.rename(columns={'code': '股票代码', 'name': '股票简称', 'pe': '市盈率', 'mcap': '总市值'}, inplace=True)
                except Exception as fe:
                    print(f"  ❌ dataapi降级失败: {fe}")
            if fallback_df is not None and not fallback_df.empty:
                self.raw_data = fallback_df
                print(f"  ✅ 降级成功！获取到 {len(fallback_df)} 只股票（无主力资金数据）")
                return True, fallback_df, f"降级成功（东财选股器）: {len(fallback_df)}只"
            error_msg = "所有查询方案+降级均失败，请检查网络或稍后重试"
            print(f"\n❌ {error_msg}")
            return False, None, error_msg
        
        except Exception as e:
            error_msg = f"获取主力选股数据失败: {str(e)}"
            print(f"\n❌ {error_msg}")
            return False, None, error_msg

    def get_main_force_stocks_cached(self, days_ago=None, start_date=None,
                                     min_market_cap=None, max_market_cap=None,
                                     use_cache: bool = True):
        """带当日缓存的主力选股(盘前预取/盘中读缓存)。
        - 盘前任务用 use_cache=False:强制现取并回写当日缓存。
        - 09:45 选股用 use_cache=True:命中当日缓存即返回,**不在高峰现调问财**。
        start_date 显式指定时不走缓存(缓存键按 days_ago 口径,避免错配)。"""
        if start_date is not None:
            return self.get_main_force_stocks(start_date=start_date, days_ago=days_ago,
                                              min_market_cap=min_market_cap, max_market_cap=max_market_cap)
        _d = 30 if days_ago is None else days_ago
        _mn = 50 if min_market_cap is None else min_market_cap
        _mx = 5000 if max_market_cap is None else max_market_cap
        key = _mf_cache_key(_d, _mn, _mx)
        if use_cache:
            cached = _mf_cache_load(key)
            if cached is not None and hasattr(cached, 'empty') and not cached.empty:
                self.raw_data = cached
                print(f"  ✅ 主力选股命中当日缓存({key}): {len(cached)} 只,不现调问财")
                return True, cached, f"命中当日缓存 {len(cached)} 只"
        ok, df, msg = self.get_main_force_stocks(days_ago=days_ago,
                                                 min_market_cap=min_market_cap, max_market_cap=max_market_cap)
        if ok and df is not None and hasattr(df, 'empty') and not df.empty:
            _mf_cache_save(key, df)
        return ok, df, msg

    def _convert_to_dataframe(self, result) -> pd.DataFrame:
        """转换问财返回结果为DataFrame"""
        try:
            if isinstance(result, pd.DataFrame):
                return result
            elif isinstance(result, dict):
                # 检查是否有嵌套的tableV1结构
                if 'tableV1' in result:
                    table_data = result['tableV1']
                    if isinstance(table_data, pd.DataFrame):
                        return table_data
                    elif isinstance(table_data, list):
                        return pd.DataFrame(table_data)
                # 直接转换字典
                return pd.DataFrame([result])
            elif isinstance(result, list):
                return pd.DataFrame(result)
            else:
                return None
        except Exception as e:
            print(f"  转换DataFrame失败: {e}")
            return None
    
    def filter_stocks(self, df: pd.DataFrame, 
                     max_range_change: float = None,
                     min_market_cap: float = None,
                     max_market_cap: float = None) -> pd.DataFrame:
        """
        智能筛选股票 - 基于涨跌幅和市值
        
        Args:
            df: 原始股票数据DataFrame
            max_range_change: 最大涨跌幅限制
            min_market_cap: 最小市值限制
            max_market_cap: 最大市值限制
            
        Returns:
            筛选后的DataFrame
        """
        if df is None or df.empty:
            return df
        
        print(f"\n{'='*60}")
        print(f"🔍 智能筛选中...")
        print(f"{'='*60}")
        print(f"筛选条件:")
        print(f"  - 区间涨跌幅 < {max_range_change}%")
        print(f"  - 市值范围: {min_market_cap}-{max_market_cap}亿")
        
        original_count = len(df)
        filtered_df = df.copy()
        
        # 1. 筛选区间涨跌幅（智能匹配列名）
        # 优先精确匹配，按优先级查找
        interval_pct_col = None
        possible_interval_pct_names = [
            '区间涨跌幅:前复权', 
            '区间涨跌幅:前复权(%)', 
            '区间涨跌幅(%)', 
            '区间涨跌幅', 
            '涨跌幅:前复权', 
            '涨跌幅:前复权(%)',
            '涨跌幅(%)',
            '涨跌幅'
        ]
        
        # 优先精确匹配
        for name in possible_interval_pct_names:
            for col in df.columns:
                if name in col:
                    interval_pct_col = col
                    break
            if interval_pct_col:
                break
        
        if interval_pct_col:
            print(f"\n使用字段: {interval_pct_col}")
            
            # 转换为数值并筛选
            filtered_df[interval_pct_col] = pd.to_numeric(filtered_df[interval_pct_col], errors='coerce')
            before = len(filtered_df)
            filtered_df = filtered_df[
                (filtered_df[interval_pct_col].notna()) & 
                (filtered_df[interval_pct_col] < max_range_change)
            ]
            print(f"  区间涨跌幅筛选: {before} -> {len(filtered_df)} 只")
        else:
            print(f"  ⚠️ 未找到区间涨跌幅字段，跳过涨跌幅筛选")
            print(f"  可用字段: {list(df.columns[:10])}")
        
        # 2. 筛选市值
        market_cap_cols = [col for col in df.columns if '总市值' in col or '市值' in col]
        if market_cap_cols:
            col_name = market_cap_cols[0]
            print(f"\n使用字段: {col_name}")
            
            # 转换为数值（单位可能是亿或元）
            filtered_df[col_name] = pd.to_numeric(filtered_df[col_name], errors='coerce')
            
            # 判断单位（如果值很大，可能是元）
            max_val = filtered_df[col_name].max()
            if max_val > 100000:  # 大于10万，认为是元
                print(f"  检测到单位为元，转换为亿")
                filtered_df[col_name] = filtered_df[col_name] / 100000000
            
            before = len(filtered_df)
            filtered_df = filtered_df[
                (filtered_df[col_name].notna()) & 
                (filtered_df[col_name] >= min_market_cap) &
                (filtered_df[col_name] <= max_market_cap)
            ]
            print(f"  市值筛选: {before} -> {len(filtered_df)} 只")
        
        # 3. 去除ST股票（额外保险）
        if '股票简称' in filtered_df.columns:
            before = len(filtered_df)
            filtered_df = filtered_df[~filtered_df['股票简称'].str.contains('ST', na=False)]
            if before != len(filtered_df):
                print(f"  ST股票过滤: {before} -> {len(filtered_df)} 只")
        
        print(f"\n筛选完成: {original_count} -> {len(filtered_df)} 只股票")
        
        self.filtered_stocks = filtered_df
        return filtered_df
    
    def get_top_stocks(self, df: pd.DataFrame, top_n: int = None) -> pd.DataFrame:
        """
        获取主力资金净流入前N名股票
        
        Args:
            df: 筛选后的股票数据
            top_n: 返回前N名
            
        Returns:
            前N名股票DataFrame
        """
        if df is None or df.empty:
            return df
        
        # 查找主力资金相关列（智能匹配）
        main_fund_col = None
        main_fund_patterns = [
            '区间主力资金流向',      # 实际列名
            '区间主力资金净流入',
            '主力资金流向',
            '主力资金净流入',
            '主力净流入'
        ]
        for pattern in main_fund_patterns:
            matching = [col for col in df.columns if pattern in col]
            if matching:
                main_fund_col = matching[0]
                break
        
        if main_fund_col:
            print(f"\n使用字段排序: {main_fund_col}")
            
            # 转换为数值并排序
            df[main_fund_col] = pd.to_numeric(df[main_fund_col], errors='coerce')
            top_df = df.nlargest(top_n, main_fund_col)
            
            print(f"获取主力资金净流入前 {len(top_df)} 名")
            return top_df
        else:
            # 如果没有主力资金列，直接返回前N条
            print(f"未找到主力资金列，返回前{top_n}条数据")
            return df.head(top_n)
    
    def format_stock_list_for_analysis(self, df: pd.DataFrame) -> List[Dict]:
        """
        格式化股票列表，准备提交给AI分析师
        
        Args:
            df: 股票数据DataFrame
            
        Returns:
            格式化后的股票列表
        """
        if df is None or df.empty:
            return []
        
        stock_list = []
        
        for idx, row in df.iterrows():
            stock_data = {
                'symbol': row.get('股票代码', 'N/A'),
                'name': row.get('股票简称', 'N/A'),
                'industry': row.get('所属同花顺行业', row.get('所属行业', 'N/A')),
                'market_cap': row.get('总市值[20241209]', row.get('总市值', 'N/A')),
                'range_change': None,
                'main_fund_inflow': None,
                'pe_ratio': row.get('市盈率', 'N/A'),
                'pb_ratio': row.get('市净率', 'N/A'),
                'revenue': row.get('营业收入', row.get('营收', 'N/A')),
                'net_profit': row.get('净利润', 'N/A'),
                'scores': {},
                'raw_data': row.to_dict()
            }
            
            # 提取区间涨跌幅（使用智能匹配）
            interval_pct_col = None
            possible_names = [
                '区间涨跌幅:前复权', '区间涨跌幅:前复权(%)', '区间涨跌幅(%)', 
                '区间涨跌幅', '涨跌幅:前复权', '涨跌幅:前复权(%)', '涨跌幅(%)', '涨跌幅'
            ]
            for name in possible_names:
                for col in df.columns:
                    if name in col:
                        interval_pct_col = col
                        break
                if interval_pct_col:
                    break
            if interval_pct_col:
                stock_data['range_change'] = row.get(interval_pct_col, 'N/A')
            
            # 提取主力资金（智能匹配）
            main_fund_col = None
            main_fund_patterns = [
                '区间主力资金流向', '区间主力资金净流入', 
                '主力资金流向', '主力资金净流入', '主力净流入'
            ]
            for pattern in main_fund_patterns:
                matching = [col for col in df.columns if pattern in col]
                if matching:
                    main_fund_col = matching[0]
                    break
            if main_fund_col:
                stock_data['main_fund_inflow'] = row.get(main_fund_col, 'N/A')
            
            # 提取评分
            score_keywords = ['评分', '能力']
            for col in df.columns:
                if any(keyword in col for keyword in score_keywords):
                    stock_data['scores'][col] = row.get(col, 'N/A')
            
            stock_list.append(stock_data)
        
        return stock_list
    
    def print_stock_summary(self, stock_list: List[Dict]):
        """打印股票摘要信息"""
        print(f"\n{'='*80}")
        print(f"📊 候选股票列表 ({len(stock_list)}只)")
        print(f"{'='*80}")
        print(f"{'序号':<4} {'代码':<8} {'名称':<12} {'行业':<15} {'主力资金':<12} {'涨跌幅':<8}")
        print(f"{'-'*80}")
        
        for i, stock in enumerate(stock_list, 1):
            symbol = stock['symbol']
            name = stock['name'][:10] if isinstance(stock['name'], str) else 'N/A'
            industry = stock['industry'][:13] if isinstance(stock['industry'], str) else 'N/A'
            
            # 格式化主力资金
            main_fund = stock['main_fund_inflow']
            if isinstance(main_fund, (int, float)):
                if abs(main_fund) >= 100000000:  # 大于1亿
                    main_fund_str = f"{main_fund/100000000:.2f}亿"
                else:
                    main_fund_str = f"{main_fund/10000:.2f}万"
            else:
                main_fund_str = 'N/A'
            
            # 格式化涨跌幅
            change = stock['range_change']
            if isinstance(change, (int, float)):
                change_str = f"{change:.2f}%"
            else:
                change_str = 'N/A'
            
            print(f"{i:<4} {symbol:<8} {name:<12} {industry:<15} {main_fund_str:<12} {change_str:<8}")
        
        print(f"{'='*80}\n")

# 全局实例
main_force_selector = MainForceStockSelector()

