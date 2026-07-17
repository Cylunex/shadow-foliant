import openai
import json
import os
import re as _re
from typing import Dict, List, Any, Optional
import config

# 买入评级要求的最小盈亏比 (目标-进场)/(进场-止损)
MIN_RISK_REWARD = 2.0
DEFAULT_STOP_PCT = 0.08   # 买入未给止损时的兜底止损幅度(entry×(1-8%)),确保每笔买入都有风险边界


def _first_num(v) -> Optional[float]:
    """从字符串/数字里抽第一个数(忽略 ¥/元/区间符号),失败 None。"""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _re.search(r'-?\d+\.?\d*', str(v).replace(',', ''))
    return float(m.group()) if m else None


def _range_mid(v) -> Optional[float]:
    """进场区间取中值:'12.5-13.0' → 12.75;单值则原样。"""
    if v is None or v == '':
        return None
    nums = _re.findall(r'\d+\.?\d*', str(v).replace(',', ''))
    if not nums:
        return None
    if len(nums) >= 2:
        return (float(nums[0]) + float(nums[1])) / 2
    return float(nums[0])


def _enforce_risk_reward(decision: Dict[str, Any], current_price=None,
                         min_rr: float = MIN_RISK_REWARD) -> Dict[str, Any]:
    """对"买入"评级做盈亏比硬约束:(目标-进场)/(进场-止损) < min_rr → 降为「持有」。

    进场价:entry_range 中值 > 当前价;目标:take_profit > target_price;止损:stop_loss。
    数字缺失/不合理(非 目标>进场>止损)时不强降,仅标注 rr_note。
    """
    rating = str(decision.get('rating', '') or '')
    if '买' not in rating and 'buy' not in rating.lower():
        return decision  # 只约束买入
    entry = _range_mid(decision.get('entry_range')) or _first_num(current_price)
    tp = _first_num(decision.get('take_profit')) or _first_num(decision.get('target_price'))
    sl = _first_num(decision.get('stop_loss'))
    # 硬约束:买入必须有止损。AI 未给/不合理(非 0<sl<entry)→ 按默认止损兜底,杜绝"无止损绕过盈亏比"。
    if entry and (not sl or sl <= 0 or sl >= entry):
        sl = round(entry * (1 - DEFAULT_STOP_PCT), 2)
        decision['stop_loss'] = sl
        decision['stop_loss_imputed'] = True
    if not (entry and tp and sl) or not (tp > entry > sl > 0):
        decision['rr_note'] = '目标/进场缺失或不合理,未校验盈亏比'
        return decision
    rr = (tp - entry) / (entry - sl)
    decision['risk_reward_ratio'] = round(rr, 2)
    if rr < min_rr:
        decision['original_rating'] = rating
        decision['rating'] = '持有'
        decision['rr_downgraded'] = True
        note = f'⚠️ 盈亏比仅 {rr:.2f}:1 (<{min_rr}:1),不值得买入 → 自动降为持有(观望)。'
        decision['risk_warning'] = note + str(decision.get('risk_warning', '') or '')
    return decision


def _extract_last_json(text: str):
    """从文本中提取**最后一个花括号配平**的 JSON 对象并解析,失败返回 None。

    替代贪婪 `\\{.*\\}`:reasoner/思考模型会把 JSON 样例写进【推理过程】,贪婪匹配会把推理段的 {
    当起点、正式答案的 } 当终点 → 拼出错位非法 JSON → json.loads 失败 → rating/目标价/止损全丢、
    盈亏比硬约束形同虚设。真正答案通常在末尾,故从最后一个 } 反向找配平的 {,逐候选尝试解析。"""
    if not text:
        return None
    end = text.rfind('}')
    while end != -1:
        depth, i, start = 0, end, -1
        while i >= 0:
            c = text[i]
            if c == '}':
                depth += 1
            elif c == '{':
                depth -= 1
                if depth == 0:
                    start = i
                    break
            i -= 1
        if start != -1:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
        end = text.rfind('}', 0, end)
    return None


class DeepSeekClient:
    """DeepSeek API客户端"""
    
    def __init__(self, model=None):
        self.model = model or config.DEFAULT_MODEL_NAME
        self.client = openai.OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            max_retries=0,   # 不让 SDK 重试放大超时;降级由 router 跨 provider 处理
        )
        self.last_used_provider: Optional[str] = None
        
    def call_api(self, messages: List[Dict[str, str]], model: Optional[str] = None,
                 temperature: float = 0.7, max_tokens: int = 2000,
                 thinking: bool = False, use_router: bool = True,
                 timeout: Optional[float] = None, call_type: str = 'misc') -> str:
        """调用 LLM。默认走 llm_router（多 provider 自动降级），保留 OpenAI 直连兜底。

        Args:
            thinking: True 时优先用 reasoner/R1/QwQ 等思考模型
            use_router: 默认 True；False 时退回旧的 DeepSeek 直连（用于单测/调试）
            timeout: 调用超时(秒);None→env LLM_TIMEOUT(默认40)。**有界超时,挡住挂起的 provider 阻塞主路径。**
        """
        model_to_use = model or self.model
        if "reasoner" in model_to_use.lower() and max_tokens <= 2000:
            max_tokens = 8000
        if thinking and max_tokens <= 2000:
            max_tokens = 8000

        if timeout is None:
            try:
                timeout = float(os.getenv('LLM_TIMEOUT', '40'))
            except (TypeError, ValueError):
                timeout = 40.0
        eff_timeout = max(timeout, 120.0) if thinking else timeout

        if use_router:
            try:
                from llm_router import get_router
                router = get_router()
                if router.providers:
                    text, used = router.call(messages, temperature=temperature,
                                             max_tokens=max_tokens, thinking=thinking,
                                             timeout=timeout, call_type=call_type)
                    if text and not text.startswith('[LLM-Router]'):
                        self.last_used_provider = used
                        return text
            except Exception as e:
                print(f"[deepseek_client] router 异常，退回直连: {e}")

        try:
            response = self.client.chat.completions.create(
                model=model_to_use,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=eff_timeout,
            )
            message = response.choices[0].message
            result = ""
            if hasattr(message, 'reasoning_content') and message.reasoning_content:
                result += f"【推理过程】\n{message.reasoning_content}\n\n"
            if message.content:
                result += message.content
            self.last_used_provider = f'deepseek-direct:{model_to_use}'
            try:  # 直连兜底路径的用量遥测(router 路径已在 llm_router 内记录)
                import llm_usage
                llm_usage.record_from_resp(call_type, f'deepseek-direct:{model_to_use}',
                                           response, thinking=thinking)
            except Exception:
                pass
            return result if result else "API返回空响应"
        except Exception as e:
            return f"API调用失败: {str(e)}"
    
    def technical_analysis(self, stock_info: Dict, stock_data: Any, indicators: Dict,
                           chan_summary: str = None, levels_summary: str = None) -> str:
        """技术面分析

        chan_summary: 可选的缠论结构摘要（来自 chan_theory.analyze_chan），传入则注入 prompt。
        levels_summary: 可选的关键价位摘要（来自 price_levels.analyze_levels），传入则注入 prompt。
        """
        chan_block = f"\n缠论结构（缠中说禅）：\n{chan_summary}\n" if chan_summary else ""
        levels_block = f"\n关键价位（量价/枢轴/通道/缺口/斐波那契）：\n{levels_summary}\n" if levels_summary else ""
        # ⭐ 缓存优化(2026-06-28):稳定框架(角色+指标口径+分析维度+输出要求)放 system —— 全批同 call_type
        # 逐字相同 → DeepSeek 上下文缓存命中;变量股票数据放 user。指标的口径说明随框架进 system,
        # user 只剩纯数值(原 7% 命中、框架在 prompt 末尾被前面变量数据顶得无法复用)。
        system = (
            "你是一名经验丰富、功底深厚的股票技术分析师。请基于 user 提供的【股票数据】做专业技术面分析。\n\n"
            "指标口径：\n"
            "- DMI 动向：ADX>25 趋势强、<20 震荡\n"
            "- ATR(14) 波动率：止损位 = 价格 - 2*ATR\n"
            "- TRIX / 信号线：金叉为强买入信号\n"
            "- ROC(12) / 均线：动量确认\n"
            "- CCI(14)：>+100 超买、<-100 超卖\n"
            "- BIAS 乖离 6/12/24：绝对值 >5 警惕回归\n"
            "- 威廉 WR(10)：>80 超卖、<20 超买\n\n"
            "请从以下角度进行分析：\n"
            "1. 趋势分析（均线系统、价格走势、ADX 强度）\n"
            "2. 超买超卖分析（RSI、KDJ、CCI、WR 多维交叉确认）\n"
            "3. 动量分析（MACD、TRIX、ROC 多周期共振）\n"
            "4. 支撑阻力分析（布林带、ATR 止损区）\n"
            "5. 乖离回归分析（BIAS 6/12/24 偏离度）\n"
            "6. 成交量分析\n"
            "7. 短期、中期、长期技术判断\n"
            "8. 关键技术位分析（若【股票数据】含关键价位：结合成交密集区/枢轴点/前高前低/缺口/斐波那契/整数关口，明确上方压力与下方支撑及其强度）\n"
            "9. 缠论分析（若【股票数据】含缠论结构：当前笔/中枢位置、背驰与一二三类买卖点，与传统指标互相印证）\n"
            "请给出专业、详细的技术分析报告，包含风险提示。"
        )
        prompt = f"""【股票数据】
股票信息：
- 股票代码：{stock_info.get('symbol', 'N/A')}
- 股票名称：{stock_info.get('name', 'N/A')}
- 当前价格：{stock_info.get('current_price', 'N/A')}
- 涨跌幅：{stock_info.get('change_percent', 'N/A')}%

最新技术指标：
- 收盘价：{indicators.get('price', 'N/A')}
- MA5：{indicators.get('ma5', 'N/A')}
- MA10：{indicators.get('ma10', 'N/A')}
- MA20：{indicators.get('ma20', 'N/A')}
- MA60：{indicators.get('ma60', 'N/A')}
- RSI：{indicators.get('rsi', 'N/A')}
- MACD：{indicators.get('macd', 'N/A')}
- MACD信号线：{indicators.get('macd_signal', 'N/A')}
- 布林带上轨：{indicators.get('bb_upper', 'N/A')}
- 布林带下轨：{indicators.get('bb_lower', 'N/A')}
- K值：{indicators.get('k_value', 'N/A')}
- D值：{indicators.get('d_value', 'N/A')}
- 量比：{indicators.get('volume_ratio', 'N/A')}

通达信级进阶指标（MyTT）：
- DMI 动向：+DI={indicators.get('pdi', 'N/A')} / -DI={indicators.get('mdi', 'N/A')} / ADX={indicators.get('adx', 'N/A')}
- ATR(14)：{indicators.get('atr', 'N/A')}
- TRIX：{indicators.get('trix', 'N/A')} / 信号线：{indicators.get('trix_ma', 'N/A')}
- ROC(12)：{indicators.get('roc', 'N/A')} / 均线：{indicators.get('roc_ma', 'N/A')}
- CCI(14)：{indicators.get('cci', 'N/A')}
- BIAS 乖离：6日={indicators.get('bias_6', 'N/A')} / 12日={indicators.get('bias_12', 'N/A')} / 24日={indicators.get('bias_24', 'N/A')}
- 威廉 WR(10)：{indicators.get('wr_10', 'N/A')}
{chan_block}{levels_block}"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]

        return self.call_api(messages, call_type='technical')
    
    def fundamental_analysis(self, stock_info: Dict, financial_data: Dict = None, quarterly_data: Dict = None) -> str:
        """基本面分析"""

        # 财务排雷方法论(借鉴 财务报表深度解读 skill)；失败则空串,不影响主流程
        # ⚠️ 2026-07-17 修:RED_FLAG_GUIDE 必须在 except 里也绑定 —— 它是函数局部名,导入失败时
        # 下方 line ~372 `... if RED_FLAG_GUIDE else ''` 会 UnboundLocalError 炸掉整只股票分析
        # (降级护栏本意"取不到就空串继续",原来反而硬崩)。forensics_block 已是死变量(重构后不用)。
        try:
            from financial_forensics import RED_FLAG_GUIDE
        except Exception:
            RED_FLAG_GUIDE = ""

        # 构建财务数据部分
        financial_section = ""
        if financial_data and not financial_data.get('error'):
            ratios = financial_data.get('financial_ratios', {})
            if ratios:
                financial_section = f"""
详细财务指标：
【盈利能力】
- 净资产收益率(ROE)：{ratios.get('净资产收益率ROE', ratios.get('ROE', 'N/A'))}
- 总资产收益率(ROA)：{ratios.get('总资产收益率ROA', ratios.get('ROA', 'N/A'))}
- 销售毛利率：{ratios.get('销售毛利率', ratios.get('毛利率', 'N/A'))}
- 销售净利率：{ratios.get('销售净利率', ratios.get('净利率', 'N/A'))}

【偿债能力】
- 资产负债率：{ratios.get('资产负债率', 'N/A')}
- 流动比率：{ratios.get('流动比率', 'N/A')}
- 速动比率：{ratios.get('速动比率', 'N/A')}

【运营能力】
- 存货周转率：{ratios.get('存货周转率', 'N/A')}
- 应收账款周转率：{ratios.get('应收账款周转率', 'N/A')}
- 总资产周转率：{ratios.get('总资产周转率', 'N/A')}

【成长能力】
- 营业收入同比增长：{ratios.get('营业收入同比增长', ratios.get('收入增长', 'N/A'))}
- 净利润同比增长：{ratios.get('净利润同比增长', ratios.get('盈利增长', 'N/A'))}

【每股指标】
- 每股收益(EPS)：{ratios.get('EPS', 'N/A')}
- 每股账面价值：{ratios.get('每股账面价值', 'N/A')}
- 股息率：{ratios.get('股息率', stock_info.get('dividend_yield', 'N/A'))}
- 派息率：{ratios.get('派息率', 'N/A')}
"""
            
            # 添加报告期信息
            if ratios.get('报告期'):
                financial_section = f"\n财务数据报告期：{ratios.get('报告期')}\n" + financial_section
        
        # 构建季报数据部分
        quarterly_section = ""
        if quarterly_data and quarterly_data.get('data_success'):
            # 使用格式化的季报数据
            from quarterly_report_data import QuarterlyReportDataFetcher
            fetcher = QuarterlyReportDataFetcher()
            quarterly_section = f"""

【最近8期季报详细数据】
{fetcher.format_quarterly_reports_for_ai(quarterly_data)}

以上是通过akshare获取的最近8期季度财务报告，请重点基于这些数据进行趋势分析。
"""
        
        # ⭐ 缓存优化(2026-06-28):稳定框架(分析维度+财务排雷方法论+输出要求)移入 system(逐字相同可缓存),
        # 变量数据(基本信息/估值/财务/季报)放 user。forensics 红旗指南随框架进 system(原条件注入,现恒定)。
        fund_framework = """请从以下维度进行专业、深入的分析：

1. **公司质地分析**
   - 业务模式和核心竞争力
   - 行业地位和市场份额
   - 护城河分析（品牌、技术、规模等）

2. **盈利能力分析**
   - ROE和ROA水平评估
   - 毛利率和净利率趋势
   - 与行业平均水平对比
   - 盈利质量和持续性

3. **财务健康度分析**
   - 资产负债结构
   - 偿债能力评估
   - 现金流状况
   - 财务风险识别

4. **成长性分析**
   - 收入和利润增长趋势
   - 增长驱动因素
   - 未来成长空间
   - 行业发展前景

5. **季报趋势分析（如有季报数据）** ⭐ 重点分析
   - **营收趋势**：分析最近8期营业收入的变化趋势，识别增长或下滑
   - **利润趋势**：分析净利润和每股收益的变化，评估盈利能力变化
   - **现金流分析**：经营现金流、投资现金流、筹资现金流的变化趋势
   - **资产负债变化**：资产规模、负债水平、所有者权益的变化
   - **季度环比/同比**：计算关键指标的环比和同比变化率
   - **经营质量**：评估收入质量、利润质量、现金流质量
   - **异常识别**：识别异常波动，分析原因（季节性、一次性事件等）
   - **趋势预判**：基于最近8期数据预判未来1-2个季度趋势

6. **估值分析**
   - 当前估值水平（PE、PB）
   - 历史估值区间对比
   - 行业估值对比
   - 结合季报趋势调整估值预期
   - 合理估值区间判断

7. **投资价值判断**
   - 综合评分（0-100分）
   - 投资亮点（特别关注季报改善趋势）
   - 投资风险（关注季报恶化信号）
   - 适合的投资者类型

8. **财务排雷与内在价值** ⭐ 新增
   - 按下方"财务排雷方法论"做杜邦分解 + 盈利质量(净利vs经营现金流) + 造假红旗排查
   - 用 DCF 思路给一个内在价值的方向性判断(两阶段:高速增长N年+永续),
     与现价比较给出"低估/合理/高估"与安全边际(数据不足时定性说明假设)

**分析要求：**
- 如果有季报数据，请重点分析8期数据的趋势变化
- 识别改善或恶化的早期信号
- 结合季报数据对未来业绩进行预判
- 数据分析要深入，结论要有依据
- 结合当前市场环境和行业发展趋势

请给出专业、详细的基本面分析报告。"""
        system = ("你是一名资深的基本面分析师，拥有CFA资格和10年以上的证券分析经验。"
                  "请基于 user 提供的【基本信息/估值/财务/季报】数据进行深入的基本面分析。\n\n"
                  + fund_framework + (("\n\n" + RED_FLAG_GUIDE) if RED_FLAG_GUIDE else ""))
        prompt = f"""【基本信息】
- 股票代码：{stock_info.get('symbol', 'N/A')}
- 股票名称：{stock_info.get('name', 'N/A')}
- 当前价格：{stock_info.get('current_price', 'N/A')}
- 市值：{stock_info.get('market_cap', 'N/A')}
- 行业：{stock_info.get('sector', 'N/A')}
- 细分行业：{stock_info.get('industry', 'N/A')}

【估值指标】
- 市盈率(PE)：{stock_info.get('pe_ratio', 'N/A')}
- 市净率(PB)：{stock_info.get('pb_ratio', 'N/A')}
- 市销率(PS)：{stock_info.get('ps_ratio', 'N/A')}
- Beta系数：{stock_info.get('beta', 'N/A')}
- 52周最高：{stock_info.get('52_week_high', 'N/A')}
- 52周最低：{stock_info.get('52_week_low', 'N/A')}
{financial_section}
{quarterly_section}"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]

        return self.call_api(messages, call_type='fundamental')
    
    def fund_flow_analysis(self, stock_info: Dict, indicators: Dict, fund_flow_data: Dict = None) -> str:
        """资金面分析"""
        
        # 构建资金流向数据部分 - 使用akshare格式化数据
        fund_flow_section = ""
        if fund_flow_data and fund_flow_data.get('data_success'):
            # 使用格式化的资金流向数据
            from fund_flow_akshare import FundFlowAkshareDataFetcher
            fetcher = FundFlowAkshareDataFetcher()
            fund_flow_section = f"""

【近20个交易日资金流向详细数据】
{fetcher.format_fund_flow_for_ai(fund_flow_data)}

以上是通过akshare从东方财富获取的实际资金流向数据，请重点基于这些数据进行趋势分析。
"""
        else:
            fund_flow_section = "\n【资金流向数据】\n注意：未能获取到资金流向数据，将基于成交量进行分析。\n"
        
        # ⭐ 缓存优化(2026-06-28):稳定框架(分析要求+8维度+分析原则)移入 system(逐字相同可缓存),
        # 变量数据(基本信息/技术指标/资金流向)放 user。
        ff_framework = """请你**基于 user 提供的【资金流向数据】中近20个交易日的完整数据**，从以下角度进行深入分析：

1. **资金流向趋势分析** ⭐ 重点
   - 分析近20个交易日主力资金的累计净流入/净流出
   - 识别资金流向的趋势性特征（持续流入、持续流出、震荡）
   - 计算主力资金净流入天数占比
   - 评估资金流向强度（累计金额、平均每日金额）

2. **主力资金行为分析** ⭐ 核心重点
   - **主力资金总体表现**：累计净流入金额、占比、趋势方向
   - **超大单分析**：机构大资金的进出动作
   - **大单分析**：主力资金的操作特征
   - **主力操作意图研判**：
     * 吸筹建仓：持续净流入 + 股价上涨/盘整
     * 派发出货：持续净流出 + 股价下跌/高位
     * 洗盘整理：震荡流入流出 + 股价调整
     * 拉升推动：集中大额流入 + 股价快速上涨

3. **散户资金行为分析**
   - **中单、小单的动向**：散户的买卖情绪
   - **主力与散户博弈**：
     * 主力流入、散户流出 → 专业资金吸筹
     * 主力流出、散户流入 → 高位接盘风险
     * 同向流动 → 趋势明确
   - 散户参与度和情绪判断

4. **量价配合分析**
   - 资金流向与股价涨跌的配合度
   - 识别量价背离：
     * 价涨量缩 + 资金流出 → 警惕顶部
     * 价跌量增 + 资金流入 → 可能见底
   - 成交活跃度变化趋势

5. **关键信号识别**
   - **买入信号**：
     * 主力持续净流入
     * 大单明显流入
     * 资金流入 + 股价上涨
   - **卖出信号**：
     * 主力持续净流出
     * 大额资金出逃
     * 资金流出 + 股价滞涨或下跌
   - **观望信号**：
     * 资金流向不明确
     * 主力与散户博弈激烈

6. **阶段性特征**
   - 早期阶段（前10个交易日）vs 近期阶段（后10个交易日）
   - 资金流向的变化趋势
   - 转折点识别

7. **投资建议**
   - 基于资金流向的操作建议
   - 关注重点和风险提示
   - 资金面对后市的指示意义
   - 未来资金流向预判

8. **投资建议**
   - 基于资金面的明确操作建议
   - 买入/持有/卖出的判断依据
   - 仓位管理建议

【分析原则】
- 主力资金持续流入 + 股价上涨 → 强势信号，主力看好
- 主力资金流出 + 股价上涨 → 警惕信号，可能是散户接盘
- 主力资金流入 + 股价下跌 → 可能是主力低位吸筹
- 主力资金流出 + 股价下跌 → 弱势信号，主力看空
- 注意区分短期波动与趋势性变化

请给出专业、详细、有深度的资金面分析报告。记住：要基于问财数据的实际内容进行分析，而不是假设！"""
        system = ("你是一名经验丰富、擅长市场资金流向和主力行为分析的资金面分析师，"
                  "能够深入解读资金数据背后的投资逻辑。\n\n" + ff_framework)
        prompt = f"""【基本信息】
股票代码：{stock_info.get('symbol', 'N/A')}
股票名称：{stock_info.get('name', 'N/A')}
当前价格：{stock_info.get('current_price', 'N/A')}
市值：{stock_info.get('market_cap', 'N/A')}

【技术指标】
- 量比：{indicators.get('volume_ratio', 'N/A')}
- 当前成交量与5日均量比：{indicators.get('volume_ratio', 'N/A')}
{fund_flow_section}"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]

        return self.call_api(messages, max_tokens=3000, call_type='fund_flow')
    
    def comprehensive_discussion(self, technical_report: str, fundamental_report: str, 
                               fund_flow_report: str, stock_info: Dict) -> str:
        """综合讨论"""
        prompt = f"""
现在需要进行一场投资决策会议，你作为首席分析师，需要综合各位分析师的报告进行讨论。

股票基本信息：
- 股票代码：{stock_info.get('symbol', 'N/A')}
- 股票名称：{stock_info.get('name', 'N/A')}
- 当前价格：{stock_info.get('current_price', 'N/A')}

技术面分析报告：
{technical_report}

基本面分析报告：
{fundamental_report}

资金面分析报告：
{fund_flow_report}

请作为首席分析师，综合以上三个维度的分析报告，进行深入讨论：

1. 各个分析维度的一致性和分歧点
2. 不同分析结论的权重考量
3. 当前市场环境下的投资逻辑
4. 潜在风险和机会识别
5. 不同投资周期的考量（短期、中期、长期）
6. 市场情绪和预期管理

请模拟一场专业的投资讨论会议，体现不同观点的碰撞和融合。
"""
        
        messages = [
            {"role": "system", "content": "你是一名资深的首席投资分析师，擅长综合不同维度的分析形成投资判断。"},
            {"role": "user", "content": prompt}
        ]

        return self.call_api(messages, max_tokens=6000, call_type='discussion')
    
    def final_decision(self, comprehensive_discussion: str, stock_info: Dict,
                      indicators: Dict, philosophy: str = None) -> Dict[str, Any]:
        """最终投资决策

        philosophy: 可选的"投资哲学透镜"复核意见(价值视角+韭菜反向),传入则纳入决策权衡。
        """
        philosophy_block = (f"\n投资哲学透镜复核(价值/长期/反向视角,作为独立参考客观纳入权衡——"
                            f"既警惕追高,也警惕因过度保守而踏空,不预设方向):\n{philosophy}\n" if philosophy else "")
        # ⭐ 缓存优化(2026-06-28):稳定框架(决策要点+评级原则+JSON格式)移入 system,变量【决策依据】放 user。
        # 决策原 0% 命中(框架在巨大的综合讨论文本之后,前缀全被顶掉)。规则/格式入 system 还利于遵从。
        system = (
            "你是一名专业的投资决策专家，需要给出明确、可执行的投资建议。"
            "请基于 user 提供的【决策依据】做出最终投资决策，必须包含以下内容：\n\n"
            "1. 投资评级：买入/持有/卖出\n"
            "2. 目标价位（具体数字）\n"
            "3. 操作建议（具体的买入/卖出策略）\n"
            "4. 进场位置（具体价位区间）\n"
            "5. 止盈位置（具体价位）\n"
            "6. 止损位置（具体价位）\n"
            "7. 持有周期建议\n"
            "8. 风险提示\n"
            "9. 仓位建议（轻仓/中等仓位/重仓）\n\n"
            "【评级原则 — 必须遵守，消除\"保守惯性\"】\n"
            "- 多空证据要**对称权衡**，不要预设保守，也不要默认看空。\n"
            "- **强势上行**（价格站上 MA20 且 ≥ MA60、MACD≥0 或翻红、出现缠论买点/底背离/超跌反弹、量化 VaR 低）→ 评级应为「买入」或「逢低买入(持有偏多)」；**不要仅因 RSI 偏高/短期超买就一律降级为「持有」**。\n"
            "- 「卖出」**仅**用于：确有趋势破位、基本面恶化、重大风险事件、明显高估等下行风险。**仅仅\"性价比不高/不够便宜/估值偏上\"应给「持有」(观望)，不要给「卖出」**——尤其对当前并未持有的标的，\"不建议买入\"≠\"卖出\"。\n"
            "- 目标价应反映**合理上行空间**，不得机械设在现价之下（除非确为高估）。\n"
            "- 若【决策依据】的讨论中出现**缠论买点、底背离、超跌反弹**等多头信号，必须作为买入依据纳入权衡，不可被\"超买\"单一理由一票否决。\n"
            "- **盈亏比硬约束**:给「买入」时,(目标价−进场价)/(进场价−止损价) **必须 ≥ 2:1**。若按合理目标/止损算不到 2:1,说明性价比不足,应改给「持有」(观望),不要勉强买入。止损可参考 进场价 − 2×ATR。\n\n"
            "请以JSON格式输出决策结果，格式如下：\n"
            "{\n"
            '    "rating": "买入/持有/卖出",\n'
            '    "target_price": "目标价位数字",\n'
            '    "operation_advice": "具体操作建议",\n'
            '    "entry_range": "进场价位区间",\n'
            '    "take_profit": "止盈价位",\n'
            '    "stop_loss": "止损价位",\n'
            '    "holding_period": "持有周期",\n'
            '    "position_size": "仓位建议",\n'
            '    "risk_warning": "风险提示",\n'
            '    "confidence_level": "信心度(1-10分)"\n'
            "}"
        )
        prompt = f"""【决策依据】
股票信息：
- 股票代码：{stock_info.get('symbol', 'N/A')}
- 股票名称：{stock_info.get('name', 'N/A')}
- 当前价格：{stock_info.get('current_price', 'N/A')}

综合分析讨论结果：
{comprehensive_discussion}
{philosophy_block}
当前关键技术位：
- MA20：{indicators.get('ma20', 'N/A')}
- 布林带上轨：{indicators.get('bb_upper', 'N/A')}
- 布林带下轨：{indicators.get('bb_lower', 'N/A')}
- ATR(14)：{indicators.get('atr', 'N/A')}（建议止损 = 价格 - 2*ATR）
- ADX 趋势强度：{indicators.get('adx', 'N/A')}（>25 趋势明确，<20 震荡）
"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]
        
        # final_decision 是唯一直接产出 rating/目标价/止损并被盈亏比硬约束校验的环节,最该用推理模型做
        # "多空对称权衡 + 盈亏比推算"。env DECISION_THINKING 控制(默认开;无 thinking_model 时 router 自动回退)。
        _think = os.getenv('DECISION_THINKING', 'true').lower() not in ('false', '0', 'no', 'off')
        response = self.call_api(messages, temperature=0.3, max_tokens=4000,
                                 call_type='decision', thinking=_think)
        
        try:
            # 解析JSON响应:取最后一个配平 JSON(应对 reasoner 把 JSON 写进【推理过程】导致贪婪正则错位)
            decision_json = _extract_last_json(response)
            if decision_json is not None:
                # 盈亏比硬约束:买入但 R:R<2 → 程序化降为持有(不只靠 prompt)
                decision_json = _enforce_risk_reward(decision_json, stock_info.get('current_price'))
                # 阶段决策护栏:盘前/非交易时段的"立即买卖"→ 加开盘确认 caveat + 降信心(随行情调整)
                try:
                    from decision_guardrail import apply_phase_guardrail
                    decision_json, _ = apply_phase_guardrail(decision_json)
                except Exception as _e:
                    print(f'[decision_guardrail] 跳过: {_e}')
                return decision_json
            else:
                # 如果无法解析JSON，返回文本响应
                return {"decision_text": response}
        except:
            return {"decision_text": response}
