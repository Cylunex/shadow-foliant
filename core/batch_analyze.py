"""批量/单股多智能体分析 —— 从原 app.py 抽出的共享逻辑(去 Streamlit 依赖)。

原 `app.py` 既是 Streamlit 入口又放了 `analyze_single_stock_for_batch`,被 portfolio_manager
等非 UI 模块引用。迁移到 WebUI、删除 app.py 前,把这段纯逻辑抽到这里,供任意调用方复用
(portfolio_manager / webui / jobs 均可 `from batch_analyze import analyze_single_stock_for_batch`)。
"""

from __future__ import annotations

import concurrent.futures

import config
from stock_data import StockDataFetcher
from ai_agents import StockAnalysisAgents
from database import db


def get_stock_data(symbol, period):
    """获取股票数据(行情+指标),30s 超时自动跳过。返回 (stock_info, stock_data_with_indicators, indicators)。"""
    fetcher = StockDataFetcher()

    def _fetch():
        info = fetcher.get_stock_info(symbol)
        data = fetcher.get_stock_data(symbol, period, adjust='qfq')  # 多智能体技术分析用前复权
        return info, data

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(_fetch)
        try:
            stock_info, stock_data = future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            print(f"⏱️ {symbol} 数据获取超时(30s),跳过")
            return {"error": "数据获取超时"}, None, None
        except Exception as e:
            print(f"❌ {symbol} 数据获取异常: {e}")
            return {"error": str(e)}, None, None

    if isinstance(stock_data, dict) and "error" in stock_data:
        return stock_info, None, None

    stock_data_with_indicators = fetcher.calculate_technical_indicators(stock_data)
    indicators = fetcher.get_latest_indicators(stock_data_with_indicators)
    return stock_info, stock_data_with_indicators, indicators


def analyze_single_stock_for_batch(symbol, period, enabled_analysts_config=None, selected_model=None):
    """单股多智能体分析(技术+基本面+资金+风险 → 讨论 → 决策),用于批量分析。
    返回 dict(success/error + stock_info/agents_results/discussion_result/final_decision)。"""
    try:
        if selected_model is None:
            selected_model = config.DEFAULT_MODEL_NAME

        if enabled_analysts_config is None:
            enabled_analysts_config = {
                'technical': True, 'fundamental': True, 'fund_flow': True,
                'risk': True, 'sentiment': False, 'news': False,
            }

        stock_info, stock_data, indicators = get_stock_data(symbol, period)
        if "error" in stock_info:
            return {"symbol": symbol, "error": stock_info['error'], "success": False}
        if stock_data is None:
            return {"symbol": symbol, "error": "无法获取股票历史数据", "success": False}

        fetcher = StockDataFetcher()
        financial_data = fetcher.get_financial_data(symbol)

        # 季报(仅A股,基本面开启时)
        quarterly_data = None
        if enabled_analysts_config.get('fundamental', True) and fetcher._is_chinese_stock(symbol):
            try:
                from quarterly_report_data import QuarterlyReportDataFetcher
                quarterly_data = QuarterlyReportDataFetcher().get_quarterly_reports(symbol)
            except Exception:
                pass

        # 资金流(可选)
        fund_flow_data = None
        if enabled_analysts_config.get('fund_flow', True) and fetcher._is_chinese_stock(symbol):
            try:
                from fund_flow_akshare import FundFlowAkshareDataFetcher
                fund_flow_data = FundFlowAkshareDataFetcher().get_fund_flow_data(symbol)
            except Exception:
                pass

        # 情绪(可选)
        sentiment_data = None
        if enabled_analysts_config.get('sentiment', False) and fetcher._is_chinese_stock(symbol):
            try:
                from market_sentiment_data import MarketSentimentDataFetcher
                sentiment_data = MarketSentimentDataFetcher().get_market_sentiment_data(symbol, stock_data)
            except Exception:
                pass

        # 新闻(可选)
        news_data = None
        if enabled_analysts_config.get('news', False) and fetcher._is_chinese_stock(symbol):
            try:
                from qstock_news_data import QStockNewsDataFetcher
                news_data = QStockNewsDataFetcher().get_stock_news(symbol)
            except Exception:
                pass

        # 风险(可选)
        risk_data = None
        if enabled_analysts_config.get('risk', True) and fetcher._is_chinese_stock(symbol):
            try:
                risk_data = fetcher.get_risk_data(symbol)
            except Exception:
                pass

        agents = StockAnalysisAgents(model=selected_model)
        agents_results = agents.run_multi_agent_analysis(
            stock_info, stock_data, indicators, financial_data,
            fund_flow_data, sentiment_data, news_data, quarterly_data, risk_data,
            enabled_analysts=enabled_analysts_config,
        )
        discussion_result = agents.conduct_team_discussion(agents_results, stock_info)
        final_decision = agents.make_final_decision(discussion_result, stock_info, indicators)

        saved_to_db, db_error, record_id = False, None, None
        try:
            record_id = db.save_analysis(
                symbol=stock_info.get('symbol', ''), stock_name=stock_info.get('name', ''),
                period=period, stock_info=stock_info, agents_results=agents_results,
                discussion_result=discussion_result, final_decision=final_decision,
            )
            saved_to_db = True
            print(f"✅ {symbol} 已保存,记录ID: {record_id}")
        except Exception as e:
            db_error = str(e)
            print(f"❌ {symbol} 保存失败: {db_error}")

        result = {
            "symbol": symbol, "success": True, "stock_info": stock_info,
            "indicators": indicators, "agents_results": agents_results,
            "discussion_result": discussion_result, "final_decision": final_decision,
            "saved_to_db": saved_to_db, "db_error": db_error,
        }
        # 抽一条决策信号(统一信号层;旁路,失败不影响分析返回)
        try:
            from decision_signal import extract_from_analysis
            extract_from_analysis(result, source_ref=str(record_id or ''))
        except Exception as _dse:
            print(f"[decision_signal] 抽取跳过: {type(_dse).__name__}: {str(_dse)[:80]}")
        return result
    except Exception as e:
        return {"symbol": symbol, "error": str(e), "success": False}
