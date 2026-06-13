"""基金模块 —— 长期 / 定投为主(场外开放式基金为主)。

子模块:
  fund_data       akshare 基金数据采集(净值/排名/经理/评级/持仓穿透,带自限流)
  fund_db         基金 DB(db_compat 统一 PG/SQLite):持有/定投计划/申赎流水/净值缓存
  fund_metrics    净值指标纯函数(年化/最大回撤/夏普/卡玛/波动)
  fund_dca        ⭐ 定投引擎:普通定投 + 移动成本 + 定投回测(vs 一次性买入)
  fund_analysis   基金综合评价打分 + 多智能体 AI 研判(复用 agents)
  fund_ui         Streamlit 页面骨架

入口先 `import _bootstrap`(项目根),再 `from fund_data import ...` 扁平 import。
"""
