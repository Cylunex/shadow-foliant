"""
季报数据获取模块
使用akshare获取个股最近8期季度财务报告
"""

import pandas as pd
import sys
import warnings
from datetime import datetime
import akshare as ak

warnings.filterwarnings('ignore')

# 设置标准输出编码为 UTF-8。
def _setup_stdout_encoding():
    """在 Windows 命令行尽量使用 UTF-8。"""
    if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='ignore')
        except (AttributeError, OSError):
            pass

_setup_stdout_encoding()


class QuarterlyReportDataFetcher:
    """季报数据获取类（使用akshare数据源）"""
    
    def __init__(self):
        self.periods = 8  # 获取最近8期季报
        self.available = True
        print("✓ 季报数据获取器初始化成功（akshare数据源）")
    
    def get_quarterly_reports(self, symbol):
        """
        获取股票的季报数据
        
        Args:
            symbol: 股票代码（6位数字）
            
        Returns:
            dict: 包含季报数据的字典
        """
        data = {
            "symbol": symbol,
            "income_statement": None,      # 利润表
            "balance_sheet": None,         # 资产负债表
            "cash_flow": None,             # 现金流量表
            "financial_indicators": None,   # 财务指标
            "data_success": False,
            "source": "akshare"
        }
        
        # 只支持中国股票
        if not self._is_chinese_stock(symbol):
            data["error"] = "季报数据仅支持中国A股股票"
            return data
        
        try:
            print(f"📊 正在获取 {symbol} 的季报数据...")
            
            # 获取利润表
            income_data = self._get_income_statement(symbol)
            if income_data:
                data["income_statement"] = income_data
                print(f"   ✓ 成功获取 {len(income_data.get('data', []))} 期利润表数据")
            
            # 获取资产负债表
            balance_data = self._get_balance_sheet(symbol)
            if balance_data:
                data["balance_sheet"] = balance_data
                print(f"   ✓ 成功获取 {len(balance_data.get('data', []))} 期资产负债表数据")
            
            # 获取现金流量表
            cash_flow_data = self._get_cash_flow(symbol)
            if cash_flow_data:
                data["cash_flow"] = cash_flow_data
                print(f"   ✓ 成功获取 {len(cash_flow_data.get('data', []))} 期现金流量表数据")
            
            # 获取财务指标
            indicators_data = self._get_financial_indicators(symbol)
            if indicators_data:
                data["financial_indicators"] = indicators_data
                print(f"   ✓ 成功获取 {len(indicators_data.get('data', []))} 期财务指标数据")
            
            # 如果至少有一个成功，则标记为成功
            if income_data or balance_data or cash_flow_data or indicators_data:
                data["data_success"] = True
                print("✅ 季报数据获取完成")
            else:
                print("⚠️ 未能获取到季报数据")
                
        except Exception as e:
            print(f"❌ 获取季报数据失败: {e}")
            data["error"] = str(e)
        
        return data
    
    def _is_chinese_stock(self, symbol):
        """判断是否为中国股票"""
        return symbol.isdigit() and len(symbol) == 6
    
    def _get_income_statement(self, symbol):
        """获取利润表数据"""
        try:
            # stock_financial_report_sina - 新浪财经季度利润表
            df = ak.stock_financial_report_sina(stock=symbol, symbol="利润表")
            
            if df is None or df.empty:
                print(f"   未找到利润表数据")
                return None
            
            # 获取最近8期
            df = df.head(self.periods)
            
            # 转换为字典列表
            data_list = []
            for idx, row in df.iterrows():
                item = {}
                for col in df.columns:
                    value = row.get(col)
                    if value is None or (isinstance(value, float) and pd.isna(value)):
                        continue
                    try:
                        item[col] = str(value)
                    except:
                        item[col] = "N/A"
                if item:
                    data_list.append(item)
            
            return {
                "data": data_list,
                "periods": len(data_list),
                "columns": df.columns.tolist(),
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
        except Exception as e:
            print(f"   获取利润表异常: {e}")
            return None
    
    def _get_balance_sheet(self, symbol):
        """获取资产负债表数据"""
        try:
            # stock_financial_report_sina - 新浪财经季度资产负债表
            df = ak.stock_financial_report_sina(stock=symbol, symbol="资产负债表")
            
            if df is None or df.empty:
                print(f"   未找到资产负债表数据")
                return None
            
            # 获取最近8期
            df = df.head(self.periods)
            
            # 转换为字典列表
            data_list = []
            for idx, row in df.iterrows():
                item = {}
                for col in df.columns:
                    value = row.get(col)
                    if value is None or (isinstance(value, float) and pd.isna(value)):
                        continue
                    try:
                        item[col] = str(value)
                    except:
                        item[col] = "N/A"
                if item:
                    data_list.append(item)
            
            return {
                "data": data_list,
                "periods": len(data_list),
                "columns": df.columns.tolist(),
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
        except Exception as e:
            print(f"   获取资产负债表异常: {e}")
            return None
    
    def _get_cash_flow(self, symbol):
        """获取现金流量表数据"""
        try:
            # stock_financial_report_sina - 新浪财经季度现金流量表
            df = ak.stock_financial_report_sina(stock=symbol, symbol="现金流量表")
            
            if df is None or df.empty:
                print(f"   未找到现金流量表数据")
                return None
            
            # 获取最近8期
            df = df.head(self.periods)
            
            # 转换为字典列表
            data_list = []
            for idx, row in df.iterrows():
                item = {}
                for col in df.columns:
                    value = row.get(col)
                    if value is None or (isinstance(value, float) and pd.isna(value)):
                        continue
                    try:
                        item[col] = str(value)
                    except:
                        item[col] = "N/A"
                if item:
                    data_list.append(item)
            
            return {
                "data": data_list,
                "periods": len(data_list),
                "columns": df.columns.tolist(),
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
        except Exception as e:
            print(f"   获取现金流量表异常: {e}")
            return None
    
    def _get_financial_indicators(self, symbol):
        """获取财务指标数据"""
        try:
            # 使用stock_financial_abstract替代已失效的stock_financial_analysis_indicator
            df = ak.stock_financial_abstract(symbol=symbol)
            
            if df is None or df.empty:
                print(f"   未找到财务指标数据")
                return None
            
            # 获取最近8期
            df = df.head(self.periods * 2)  # 取更多数据以确保有足够的季度数据
            
            # 提取关键财务指标
            key_indicators = [
                '净资产收益率(ROE)', '总资产报酬率(ROA)', '销售净利率', '销售毛利率',
                '资产负债率', '流动比率', '速动比率', '应收账款周转率', '存货周转率',
                '总资产周转率', '基本每股收益', '每股净资产', '每股现金流'
            ]
            
            # 筛选出包含关键指标的行
            indicator_rows = df[df['指标'].isin(key_indicators)]
            
            if indicator_rows.empty:
                print(f"   未找到关键财务指标数据")
                return None
            
            # 获取日期列（排除'选项'和'指标'列）
            date_columns = [col for col in df.columns if col not in ['选项', '指标']]
            
            # 转换为字典列表，每个字典代表一个时期的财务指标
            data_list = []
            for date_col in date_columns[:self.periods]:  # 只取最近的periods期
                item = {'报告期': date_col}
                for _, row in indicator_rows.iterrows():
                    indicator_name = row['指标']
                    value = row.get(date_col)
                    if value is not None and not (isinstance(value, float) and pd.isna(value)):
                        try:
                            # 尝试转换为字符串
                            item[indicator_name] = str(value)
                        except:
                            item[indicator_name] = "N/A"
                    else:
                        item[indicator_name] = "N/A"
                data_list.append(item)
            
            return {
                "data": data_list,
                "periods": len(data_list),
                "columns": ['报告期'] + key_indicators,
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
        except Exception as e:
            print(f"   获取财务指标异常: {e}")
            return None
    
    def format_quarterly_reports_for_ai(self, data):
        """
        将季报数据格式化为适合AI阅读的文本
        """
        if not data or not data.get("data_success"):
            return "未能获取季报数据"
        
        text_parts = []
        text_parts.append(f"""
【季度财务报告数据 - akshare数据源】
股票代码：{data.get('symbol', 'N/A')}
数据期数：最近{self.periods}期季报

""")
        
        # 利润表数据
        if data.get("income_statement"):
            income_data = data["income_statement"]
            text_parts.append(f"""
═══════════════════════════════════════
📊 利润表（最近{income_data.get('periods', 0)}期）
═══════════════════════════════════════
""")
            
            # 提取关键指标
            key_fields = ['报告期', '营业总收入', '营业收入', '营业总成本', '营业利润', 
                         '利润总额', '净利润', '归属于母公司所有者的净利润', 
                         '基本每股收益', '稀释每股收益']
            
            for idx, item in enumerate(income_data.get('data', []), 1):
                text_parts.append(f"\n第 {idx} 期:")
                for field in key_fields:
                    if field in item:
                        text_parts.append(f"  {field}: {item[field]}")
                
                # 显示其他重要字段（如果有）
                other_fields = ['销售费用', '管理费用', '财务费用', '研发费用']
                for field in other_fields:
                    if field in item:
                        text_parts.append(f"  {field}: {item[field]}")
        
        # 资产负债表数据
        if data.get("balance_sheet"):
            balance_data = data["balance_sheet"]
            text_parts.append(f"""

═══════════════════════════════════════
📊 资产负债表（最近{balance_data.get('periods', 0)}期）
═══════════════════════════════════════
""")
            
            # 提取关键指标
            key_fields = ['报告期', '资产总计', '流动资产合计', '非流动资产合计',
                         '负债合计', '流动负债合计', '非流动负债合计',
                         '所有者权益合计', '归属于母公司股东权益合计']
            
            for idx, item in enumerate(balance_data.get('data', []), 1):
                text_parts.append(f"\n第 {idx} 期:")
                for field in key_fields:
                    if field in item:
                        text_parts.append(f"  {field}: {item[field]}")
        
        # 现金流量表数据
        if data.get("cash_flow"):
            cash_flow_data = data["cash_flow"]
            text_parts.append(f"""

═══════════════════════════════════════
📊 现金流量表（最近{cash_flow_data.get('periods', 0)}期）
═══════════════════════════════════════
""")
            
            # 提取关键指标
            key_fields = ['报告期', '经营活动产生的现金流量净额', 
                         '投资活动产生的现金流量净额', '筹资活动产生的现金流量净额',
                         '现金及现金等价物净增加额', '期末现金及现金等价物余额']
            
            for idx, item in enumerate(cash_flow_data.get('data', []), 1):
                text_parts.append(f"\n第 {idx} 期:")
                for field in key_fields:
                    if field in item:
                        text_parts.append(f"  {field}: {item[field]}")
        
        # 财务指标数据
        if data.get("financial_indicators"):
            indicators_data = data["financial_indicators"]
            text_parts.append(f"""

═══════════════════════════════════════
📊 关键财务指标（最近{indicators_data.get('periods', 0)}期）
═══════════════════════════════════════
""")
            
            # 提取关键指标
            key_fields = ['报告期', '净资产收益率', '总资产净利率', '销售净利率',
                         '销售毛利率', '资产负债率', '流动比率', '速动比率',
                         '应收账款周转率', '存货周转率', '总资产周转率',
                         '每股收益', '每股净资产', '每股经营现金流']
            
            for idx, item in enumerate(indicators_data.get('data', []), 1):
                text_parts.append(f"\n第 {idx} 期:")
                for field in key_fields:
                    if field in item:
                        text_parts.append(f"  {field}: {item[field]}")
        
        return "\n".join(text_parts)


# 测试函数
if __name__ == "__main__":
    print("测试季报数据获取（akshare数据源）...")
    print("="*60)
    
    fetcher = QuarterlyReportDataFetcher()
    
    if not fetcher.available:
        print("❌ 季报数据获取器不可用")
        sys.exit(1)
    
    # 测试股票
    test_symbols = ["000001", "600519"]  # 平安银行、贵州茅台
    
    for symbol in test_symbols:
        print(f"\n{'='*60}")
        print(f"正在测试股票: {symbol}")
        print(f"{'='*60}\n")
        
        data = fetcher.get_quarterly_reports(symbol)
        
        if data.get("data_success"):
            print("\n" + "="*60)
            print("季报数据获取成功！")
            print("="*60)
            
            formatted_text = fetcher.format_quarterly_reports_for_ai(data)
            print(formatted_text)
        else:
            print(f"\n获取失败: {data.get('error', '未知错误')}")
        
        print("\n")

